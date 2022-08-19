import io
from typing import Any, Callable, Dict, Iterable, Iterator, List, Tuple, TypeVar

import torch
from torch import Tensor
from torch.nn import Module
from torch.nn.parameter import Parameter
from torch.utils.data import Dataset

from tqdm import tqdm
from time import sleep

from bastionai.pb.remote_torch_pb2 import Chunk, ClientInfo, Metric

T = TypeVar("T")
U = TypeVar("U")
SIZE_LEN = 8


def parametrized_modules(module: Module) -> Iterable[Module]:
    """Recursively iterates over all submodules.

    The method returns submodules that have parameters
    (as opposed to "wrapper modules" that just organize modules).

    Args:
        module (Module): torch.nn.Module

    Returns:
        Iterable[Module]:
    """
    yield from (
        (m_name, m)  # type: ignore
        for (m_name, m) in module.named_modules()
        if any(p is not None for p in m.parameters(recurse=False))
    )


def trainable_modules(module: Module) -> Iterable[Tuple[str, Module]]:
    """Recursively iterates over all submodules.

    The method returns submodules that have parameters and are trainable.

    Args:
        module (Module): torch.nn.Module

    Returns:
        Iterable[Tuple[str, Module]]:
    """
    yield from (
        (m_name, m)
        for (m_name, m) in parametrized_modules(module)  # type: ignore
        if any(p.requires_grad for p in m.parameters(recurse=False))
    )


def trainable_parameters(module: Module) -> Iterable[Tuple[str, Parameter]]:
    """Recursively iterates over all parameters.

    It returns submodules that are trainable (ie they want a grad).

    Args:
        module (Module): torch.nn.Module

    Returns:
        Iterable[Tuple[str, Parameter]]:
    """
    yield from (
        (p_name, p) for (p_name, p) in module.named_parameters() if p.requires_grad
    )


class DataWrapper(Module):
    def __init__(self, columns: List[torch.Tensor], labels: torch.Tensor) -> None:
        super().__init__()
        for i, column in enumerate(columns):
            self.__setattr__(f"samples_{i}", Parameter(
                column, requires_grad=False))
        self.labels = Parameter(labels, requires_grad=False)


class TensorDataset(Dataset):
    def __init__(self, columns: List[torch.Tensor], labels: torch.Tensor) -> None:
        super().__init__()
        self.columns = columns
        self.labels = labels

    def __len__(self) -> int:
        return len(self.labels)

    def __getitem__(self, idx: int) -> Tuple[List[torch.Tensor], torch.Tensor]:
        return ([column[idx] for column in self.columns], self.labels[idx])


def dataset_from_chunks(chunks: Iterator[Chunk]) -> TensorDataset:
    wrapper = list(
        unstream_artifacts(
            (chunk.data for chunk in chunks), deserialization_fn=torch.jit.load
        )
    )[
        0
    ]  # type: ignore
    columns = []
    labels = None
    for name, param in wrapper.named_parameters():
        if name == "labels":
            labels = param
        elif name.startswith("samples_"):
            idx = int(name[8:])
            if len(columns) < idx:
                columns += [None] * (idx + 1 - len(columns))
            columns[idx] = param
        else:
            raise Exception(f"Unknown field {name} in data wrapper")
    if len(columns) == 0:
        raise Exception(f"Data wrapper must contain at least one column.")
    if any([x is None for x in columns]):
        raise Exception(f"Missing column in data wrapper.")

    return TensorDataset(columns, labels)


def chunks(
    it: Iterator[T], chunk_size: int, cat_fn: Callable[[List[T]], U] = lambda x: x
) -> Iterator[U]:
    chunk = []
    for x in it:
        if len(chunk) == chunk_size:
            yield cat_fn(chunk)
            chunk = [x]
        else:
            chunk.append(x)
    if len(chunk) > 0:
        yield cat_fn(chunk)


def tensor_to_bytes(tensor: Tensor) -> bytes:
    buff = io.BytesIO()
    torch.save(tensor, buff)
    buff.seek(0)
    return buff.read()


def bytes_to_tensor(bs: bytes) -> Tensor:
    buff = io.BytesIO()
    buff.write(bs)
    buff.seek(0)
    tensor = torch.load(buff)
    return tensor


def serialize_batch(data: Tuple[List[torch.Tensor], torch.Tensor], buff):
    torch.jit.save(torch.jit.script(DataWrapper(*data)), buff)


def stream_artifacts(
    artifacts: Iterator[T],
    chunk_size: int,
    serialization_fn: Callable[[T, io.BytesIO], None] = torch.save,
) -> Iterator[bytes]:
    eoi = False
    buff = io.BytesIO()
    while not eoi:
        while buff.tell() < chunk_size:
            try:
                artifact = artifacts.__next__()
                header = buff.tell()
                buff.write(b"\xde\xad\xbe\xef\xde\xad\xbe\xef")
                serialization_fn(artifact, buff)
                end = buff.tell()
                buff.seek(header)
                buff_len = (end - header - SIZE_LEN).to_bytes(
                    SIZE_LEN, byteorder="little"
                )
                buff.write(buff_len)
                buff.seek(end)
            except StopIteration:
                eoi = True
                break
        position = buff.tell()
        buff.seek(0)
        if eoi:
            yield buff.read(position)
        else:
            yield buff.read(chunk_size)
            tail = buff.read(position - chunk_size)
            buff.seek(0)
            buff.write(tail)


def unstream_artifacts(
    stream: Iterator[bytes], deserialization_fn: Callable[[io.BytesIO], T] = torch.load
) -> Iterator[T]:
    buff = io.BytesIO()
    init_chunk = stream.__next__()
    size = int.from_bytes(init_chunk[:8], "little")
    buff.write(init_chunk[8:])
    eoi = False

    while not eoi:
        while buff.tell() < size + 4:
            try:
                buff.write(stream.__next__())
            except StopIteration:
                buff.seek(0)
                yield deserialization_fn(buff)
                eoi = True
                break

        if not eoi:
            end = buff.tell()
            buff.seek(size)
            header = buff.read(SIZE_LEN)
            size = int.from_bytes(header, "little")
            tail = buff.read(end - size - SIZE_LEN)
            buff.seek(0)
            yield deserialization_fn(buff)
            buff.seek(0)
            buff.write(tail)


def data_chunks_generator(
    stream: Iterator[bytes], description: str, secret: bytes, client_info: ClientInfo,  dataset_name: str = '', model_name: str = ''
) -> Iterator[Chunk]:
    first = True
    for x in stream:
        if first:
            first = False
            yield Chunk(
                data=x,
                description=description,
                dataset_name=dataset_name,
                model_name=model_name,
                secret=secret,
                client_info=client_info)
        else:
            yield Chunk(data=x,
                        description="",
                        secret=bytes(),
                        client_info=(),
                        dataset_name='',
                        model_name='',
                        )


def make_batch(data: List[Tuple[List[Tensor], Tensor]]) -> Tuple[Tensor, Tensor]:
    return (
        [torch.stack([x[0][i] for x in data]) for i in range(len(data[0][0]))],
        torch.stack([x[1] for x in data]),
    )


def serialize_dataset(
    dataset: Dataset,
    description: str,
    dataset_name: str,
    secret: bytes,
    client_info: ClientInfo,
    chunk_size=100_000_000,
    batch_size=1024,
) -> Iterator[Chunk]:
    return data_chunks_generator(
        stream_artifacts(
            chunks(iter(dataset), batch_size, cat_fn=make_batch),
            chunk_size,
            serialization_fn=serialize_batch,
        ),
        description,
        secret,
        client_info,
        dataset_name=dataset_name,
    )


def serialize_model(
    model: Module, description: str, model_name: str, secret: bytes, client_info: ClientInfo, chunk_size=100_000_000,
) -> Iterator[Chunk]:
    ts = torch.jit.script(model)  # type: ignore
    # type: ignore
    return data_chunks_generator(
        stream_artifacts(iter([ts]), chunk_size, torch.jit.save),
        description,
        secret,
        client_info,
        model_name=model_name,
    )


def deserialize_weights_to_model(model: Module, chunks: Iterator[Chunk]) -> None:
    wrapper = list(
        unstream_artifacts(
            (chunk.data for chunk in chunks), deserialization_fn=torch.jit.load
        )
    )[
        0
    ]  # type: ignore
    for name, value in wrapper.named_parameters():
        param = model
        parent = None
        segments = name.split("_")
        name_buf = []
        for segment in segments:
            name_buf.append(segment)
            name = "_".join(name_buf)
            if hasattr(param, name):
                parent = param
                param = param.__getattr__(name)
                name_buf = []

        parent.__setattr__(name, torch.nn.Parameter(value))


def metric_tqdm_with_epochs(metric_stream: Iterator[Metric], name: str):
    def new_tqdm_bar(epoch: int, nb_epochs, nb_batches):
        t = tqdm(
            total=nb_batches,
            unit="batch",
            bar_format="{l_bar}{bar:20}{r_bar}",
        )
        t.set_description("Epoch {}/{} - train".format(epoch, nb_epochs))
        return t

    t = None
    for metric in metric_stream:
        if t is None:
            t = new_tqdm_bar(1, metric.nb_epochs, metric.nb_batches)
        t.update()
        t.set_postfix(**{name: "{:.4f}".format(metric.value)})
        if metric.batch == metric.nb_batches - 1:
            t.close()
            if metric.epoch < metric.nb_epochs - 1:
                t = new_tqdm_bar(metric.epoch + 2,
                                 metric.nb_epochs, metric.nb_batches)


def metric_tqdm(metric_stream: Iterator[Metric], name: str):
    with tqdm(
        metric_stream,
        unit="batch",
        bar_format="{l_bar}{bar:20}{r_bar}",
    ) as t:
        t.set_description("Test")

        for metric in t:
            if t.total is None:
                t.total = metric.nb_batches
            t.set_postfix(**{name: "{:.4f}".format(metric.value)})


class MultipleOutputWrapper(Module):
    def __init__(self, module: Module, output: int = 0) -> None:
        super().__init__()
        self.inner = module
        self.output = output

    def forward(self, *args, **kwargs) -> Tensor:
        output = self.inner.forward(*args, **kwargs)
        return output[self.output]
