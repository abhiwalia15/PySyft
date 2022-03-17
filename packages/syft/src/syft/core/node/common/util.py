# stdlib
from io import BytesIO
from typing import Any
from typing import Generator
from typing import List
from typing import Optional

# third party
import boto3
from botocore.client import Config
from pydantic import BaseSettings
import requests

# relative
from ....grid import GridURL
from ....util import size_mb
from ...common.serde.serialize import _serialize as serialize
from ...common.uid import UID
from ...store.proxy_dataset import ProxyDataClass


def read_chunks(
    fp: BytesIO, chunk_size: int = 1024**3
) -> Generator[bytes, None, None]:
    """Read data in chunks from the file."""
    while True:
        data = fp.read(chunk_size)
        if not data:
            break
        yield data


def listify(x: Any) -> List[Any]:
    """turns x into a list.
    If x is a list or tuple, return as list.
    if x is not a list: return [x]
    if x is None: return []

    Args:
        x (Any): some object

    Returns:
        List[Any]: x, as a list
    """
    return list(x) if isinstance(x, (list, tuple)) else ([] if x is None else [x])


def upload_result_to_s3(
    asset_name: str,
    dataset_name: str,
    domain_id: UID,
    data: Any,
    settings: BaseSettings,
) -> ProxyDataClass:
    """Upload data to Seaweed using boto3 client.

    - Serialize data to binary
    - Upload data to Seaweed using boto3 client
    - Create a ProxyDataClass to store the metadata of the data uploaded to Seaweed

    Args:
        asset_name (str): name of the data being uploaded to Seaweed
        dataset_name (str): name of the dataset to which the data belongs
        domain_id (UID): unique id of the domain node
        data (Any): data to be uploaded to Seaweed
        settings (BaseSettings): base settings of the PyGrid server

    Returns:
        ProxyDataClass: Class to store the metadata of the data being uploaded
    """

    s3_client = get_s3_client(settings=settings)

    binary_dataset: bytes = serialize(data, to_bytes=True)  # type: ignore

    # 1- Serialize the data to be uploaded to bytes.
    binary_buffer = BytesIO(binary_dataset)
    filename = f"{dataset_name}/{asset_name}"

    # 2 - Start to upload binary data into Seaweed.
    upload_response = s3_client.put_object(
        Bucket=domain_id.no_dash,
        Body=binary_buffer,
        Key=filename,
        ContentType="application/octet-stream",
    )

    # TODO: Throw an exception if the response is not valid
    print("Upload Result")
    print(upload_response)

    # 3 - Create a ProxyDataClass for the given data
    data_dtype = str(type(data))
    proxy_obj = ProxyDataClass(
        asset_name=asset_name,
        dataset_name=dataset_name,
        node_id=domain_id,
        dtype=data_dtype,
        shape=data.shape,
    )
    return proxy_obj


def upload_to_s3_using_presigned(
    client: Any,
    data: Any,
    chunk_size: int,
    asset_name: str,
    dataset_name: Optional[str] = None,
) -> ProxyDataClass:
    """Perform a multipart upload of data to Seaweed using boto3 presigned urls.

    The main steps involve:
    - Converting the data to binary data
    - Chunking the binary into smaller chunks
    - Create presigned urls for each chunk
    - Upload data to Seaweed via PUT request
    - Send a acknowledge to Seaweed via PyGrid when all chunks are successfully uploaded
    - Create a ProxyDataClass to store metadata of the uploaded data

    Args:
        client (Any): Client to send object to
        data (Any): Data to be uploaded to Seaweed
        chunk_size (int): smallest size of the data to be uploaded in bytes
        asset_name (str): name of the data being uploaded
        dataset_name Optional[(str)]: name of the dataset to which the data belongs

    Raises:
        Exception: If upload of data chunks to Seaweed fails.

    Returns:
        ProxyDataClass: Class to store metadata about the data that is uploaded to Seaweed.
    """

    # relative
    from .node_service.upload_service.upload_service_messages import (
        UploadDataCompleteMessage,
    )
    from .node_service.upload_service.upload_service_messages import UploadDataMessage

    dataset_name = dataset_name if dataset_name is not None else ""

    # Step 1 - Convert data to be uploaded to binary
    binary_dataset: bytes = serialize(data, to_bytes=True)  # type: ignore
    file_size = len(binary_dataset)

    # Step 2 - Send a message to PyGrid to inform of the data being uploaded,
    # and get presigned url for each chunk of data being uploaded.
    upload_response = client.datasets.perform_api_request_generic(
        syft_msg=UploadDataMessage,
        content={
            "filename": f"{dataset_name}/{asset_name}",
            "file_size": file_size,
            "chunk_size": chunk_size,
            "address": client.address,
            "reply_to": client.address,
        },
    )

    # Step 3 - Starts to upload binary data into Seaweed.
    binary_buffer = BytesIO(binary_dataset)
    parts = sorted(upload_response.payload.parts, key=lambda x: x["part_no"])
    etag_chunk_no_pairs = list()
    for data_chunk, part in zip(read_chunks(binary_buffer, chunk_size), parts):
        presigned_url = part["url"]
        part_no = part["part_no"]
        client_url = client.url_from_path(presigned_url)
        part["client_url"] = client_url

        res = requests.put(client_url, data=data_chunk)

        # TODO: Replace with some error message if it fails.

        if res.status_code != 200:
            raise Exception(
                f"Uploading Chunk {part} failed. "
                + f"HTTP Status Code: {res.status_code}"
            )
        etag = res.headers["ETag"]
        etag_chunk_no_pairs.append(
            {"ETag": etag, "PartNumber": part_no}
        )  # maintain list of part no and ETag

    # Step 4 - Send a message to PyGrid informing about dataset upload complete!
    upload_response = client.datasets.perform_request(
        syft_msg=UploadDataCompleteMessage,
        content={
            "upload_id": upload_response.payload.upload_id,
            "filename": f"{dataset_name}/{asset_name}",
            "parts": etag_chunk_no_pairs,
        },
    )

    # Step 5 - Create a proxy dataset for the uploaded data.
    data_dtype = str(type(data))
    proxy_data = ProxyDataClass(
        asset_name=asset_name,
        dataset_name=dataset_name,
        node_id=client.id,
        dtype=data_dtype,
        shape=data.shape,
    )

    return proxy_data


def get_s3_client(settings: BaseSettings = BaseSettings()) -> "boto3.client.S3":
    try:
        s3_endpoint = settings.S3_ENDPOINT
        s3_port = settings.S3_PORT
        s3_grid_url = GridURL(host_or_ip=s3_endpoint, port=s3_port)
        return boto3.client(
            "s3",
            endpoint_url=s3_grid_url.url,
            aws_access_key_id=settings.S3_ROOT_USER,
            aws_secret_access_key=settings.S3_ROOT_PWD,
            config=Config(signature_version="s3v4"),
            region_name=settings.S3_REGION,
        )
    except Exception as e:
        print(f"Failed to create S3 Client with {s3_endpoint} {s3_port} {s3_grid_url}")
        raise e


def check_send_to_blob_storage(
    obj: Any, settings: Optional[BaseSettings] = None, use_blob_storage: bool = False
) -> bool:
    """Check if the data needs to be send to Seaweed storage depending upon its size and type.

    Args:
        obj (Any): Data to be stored to Seaweed.
        settings (Optional[BaseSettings], optional): domain client settings. Defaults to None.
        use_blob_storage (bool, optional): Explicit flag to send the data to blob storage. Defaults to False.

    Returns:
        bool: _description_
    """
    # relative
    from ...tensor.autodp.ndim_entity_phi import NDimEntityPhiTensor as NDEPT

    # Environment variables take precedence is provided
    if hasattr(settings, "USE_BLOB_STORAGE"):
        use_blob_storage = settings.USE_BLOB_STORAGE  # type: ignore

    if use_blob_storage and isinstance(obj, NDEPT) or size_mb(obj) > 1:
        return True
    return False
