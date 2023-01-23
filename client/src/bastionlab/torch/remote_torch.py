import torch
import hashlib
import io
from typing import Iterator, TYPE_CHECKING, List, Optional
from dataclasses import dataclass
from .utils import DataWrapper, Chunk, TensorDataset
from ..pb.bastionlab_torch_pb2 import UpdateTensor, RemoteDatasetReference
from ..pb.bastionlab_pb2 import Reference
from torch.utils.data import Dataset, DataLoader
from ..pb.bastionlab_pb2 import Reference, TensorMetaData
import logging

if TYPE_CHECKING:
    from ..client import Client


@dataclass
class RemoteTensor:
    """
    BastionLab reference to a PyTorch (tch) Tensor on the server.

    It also stores a few basic information about the tensor (`dtype`, `shape`).

    You can also change the dtype of the tensor through an API call
    """

    _client: "Client"
    _identifier: str
    _dtype: torch.dtype
    _shape: torch.Size

    @property
    def identifier(self) -> str:
        return self._identifier

    def _serialize(self) -> str:
        return f'{{"identifier": "{self.identifier}"}}'

    @staticmethod
    def _send_tensor(client: "Client", tensor: torch.Tensor) -> "RemoteTensor":
        tensor = TensorDataset([], tensor)
        dataset = RemoteDataset._from_dataset(client, tensor, name=None)
        return dataset.labels

    @staticmethod
    def _from_reference(ref: Reference, client: "Client") -> "RemoteTensor":
        dtypes, shape = _get_tensor_metadata(ref.meta)
        return RemoteTensor(client, ref.identifier, dtypes[0], shape[0])

    def __str__(self) -> str:
        return f"RemoteTensor(identifier={self._identifier}, dtype={self._dtype}, shape={self._shape})"

    def __repr__(self) -> str:
        return str(self)

    @property
    def dtype(self) -> torch.dtype:
        """Returns the torch dtype of the corresponding tensor"""
        return self._dtype

    @property
    def shape(self):
        """Returns the torch Size of the corresponding tensor"""
        return self._shape

    def to(self, dtype: torch.dtype):
        """
        Performs Tensor dtype conversion.

        Args:
            dtype: torch.dtype
                The resulting torch.dtype
        """
        res = self._client.torch.stub.ModifyTensor(
            UpdateTensor(identifier=self.identifier, dtype=tch_kinds[dtype])
        )
        return RemoteTensor._from_reference(res, self._client)


torch_dtypes = {
    "Int8": torch.uint8,
    "UInt8": torch.uint8,
    "Int16": torch.int16,
    "Int32": torch.int32,
    "Int64": torch.int64,
    "Half": torch.half,
    "Float": torch.float,
    "Float32": torch.float32,
    "Float64": torch.float64,
    "Double": torch.double,
    "ComplexHalf": torch.complex32,
    "ComplexFloat": torch.complex64,
    "ComplexDouble": torch.complex128,
    "Bool": torch.bool,
    "QInt8": torch.qint8,
    "QInt32": torch.qint32,
    "BFloat16": torch.bfloat16,
}

tch_kinds = {v: k for k, v in torch_dtypes.items()}


def _get_tensor_metadata(meta_bytes: bytes):
    meta = TensorMetaData()
    meta.ParseFromString(meta_bytes)

    return [torch_dtypes[dt] for dt in meta.input_dtype], [
        torch.Size(list(meta.input_shape))
    ]


def _tracer(dtypes: List[torch.dtype], shapes: List[torch.Size]):
    return [
        torch.zeros(shape[-1], dtype=dtype)
        if dtype in [torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64]
        else torch.randn(shape[-1], dtype=dtype)
        for shape, dtype in zip(shapes, dtypes)
    ]


@dataclass
class RemoteDataset:
    inputs: List[RemoteTensor]
    labels: RemoteTensor
    name: Optional[str] = "RemoteDataset"
    description: Optional[str] = "RemoteDataset"
    privacy_limit: Optional[float] = -1.0

    @property
    def _trace_input(self):
        dtypes = [input.dtype for input in self.inputs]
        shapes = [input.shape for input in self.inputs]
        return _tracer(dtypes, shapes)

    @property
    def nb_samples(self) -> int:
        """
        Returns the number of samples in the RemoteDataset
        """
        return self.labels.shape[0]

    @staticmethod
    def _from_dataset(
        client: "Client", dataset: Dataset, *args, **kwargs
    ) -> "RemoteDataset":
        res: RemoteDatasetReference = client.send_dataset(dataset, *args, **kwargs)
        inputs = [RemoteTensor._from_reference(ref, client) for ref in res.inputs]
        labels = RemoteTensor._from_reference(res.labels, client)

        return RemoteDataset(
            inputs,
            labels,
            name=kwargs.get("name"),
            description=kwargs.get("description"),
            privacy_limit=kwargs.get("privacy_limit"),
        )

    def _serialize(self):
        inputs = ",".join([input._serialize() for input in self.inputs])
        return f'{{"inputs": [{inputs}], "labels": {self.labels._serialize()}, "nb_samples": {self.nb_samples}, "privacy_limit": {self.privacy_limit}}}'

    def __str__(self) -> str:
        return f"RemoteDataset(name={self.name}, privacy_limit={self.privacy_limit}, inputs={str(self.inputs)}, label={str(self.labels)})"
