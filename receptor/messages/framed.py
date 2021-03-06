"""
This module provides classes to build framed messages as well as consume a
stream of framed messages into descrete messages.

There are two configurations of framed messages, single and dual part::

    FramedMessage--------------------------------
        Frame (Header)
        {json data}
        Frame (Payload)
        FileBackedBuffer
    ---------------------------------------------

    FramedMessage--------------------------------
        Frame (Command)
        {json data}
    ---------------------------------------------
"""
import asyncio
import functools
import io
import logging
import os
import struct
import tempfile
import uuid
from enum import IntEnum

from .. import serde as json
from ..exceptions import ReceptorRuntimeError

logger = logging.getLogger(__name__)

MAX_INT64 = 2 ** 64 - 1


class Frame:
    """
    A Frame represents the minimal metadata about a transmission.

    Usually you should not create one directly, but rather use the
    FramedMessage class.
    """

    class Types(IntEnum):
        HEADER = 0
        PAYLOAD = 1
        COMMAND = 2

    fmt = struct.Struct(">ccIIQQ")

    __slots__ = ("type", "version", "length", "msg_id", "id")

    def __init__(self, type_, version, length, msg_id, id_):
        self.type = type_
        self.version = version
        self.length = length
        self.msg_id = msg_id
        self.id = id_

    def __repr__(self):
        return f"Frame({self.type}, {self.version}, {self.length}, {self.msg_id}, {self.id})"

    def serialize(self):
        return self.fmt.pack(
            bytes([self.type]),
            bytes([self.version]),
            self.id,
            self.length,
            *split_uuid(self.msg_id),
        )

    @classmethod
    def deserialize(cls, buf):
        t, v, i, length, hi, lo = Frame.fmt.unpack(buf)
        msg_id = join_uuid(hi, lo)
        return cls(Frame.Types(ord(t)), ord(v), length, msg_id, i)

    @classmethod
    def from_data(cls, data):
        return cls.deserialize(data[: Frame.fmt.size]), data[Frame.fmt.size :]

    @classmethod
    def wrap(cls, data, type_=Types.PAYLOAD, msg_id=None):
        """
        Returns a frame for the passed data.
        """
        if not msg_id:
            msg_id = uuid.uuid4().int

        return cls(type_, 1, len(data), msg_id, 1)


def split_uuid(data):
    "Splits a 128 bit int into two 64 bit words for binary encoding"
    return ((data >> 64) & MAX_INT64, data & MAX_INT64)


def join_uuid(hi, lo):
    "Joins two 64 bit words into a 128bit int from binary encoding"
    return (hi << 64) | lo


class FileBackedBuffer:
    def __init__(self, fp, length=0, min_chunk=2 ** 12, max_chunk=2 ** 20):
        self.length = length
        self.fp = fp
        self._min_chunk = min_chunk
        self._max_chunk = max_chunk

    @classmethod
    def from_temp(cls, dir=None, delete=True):
        return cls(tempfile.NamedTemporaryFile(dir=dir, delete=delete))

    @classmethod
    def from_buffer(cls, buffered_io, dir=None, delete=False):
        if not isinstance(buffered_io, io.BytesIO):
            raise ReceptorRuntimeError("buffer must be of type io.BytesIO")
        return cls(fp=buffered_io, length=buffered_io.getbuffer().nbytes)

    @classmethod
    def from_data(cls, raw_data, dir=None, delete=True):
        if isinstance(raw_data, str):
            raw_data = raw_data.encode()
        fbb = cls.from_temp(dir=dir, delete=delete)
        fbb.write(raw_data)
        return fbb

    @classmethod
    def from_dict(cls, raw_data, dir=None, delete=True):
        try:
            d = json.dumps(raw_data).encode("utf-8")
        except Exception as e:
            raise ReceptorRuntimeError("failed to encode raw data into json") from e
        fbb = cls.from_temp(dir=dir, delete=delete)
        fbb.write(d)
        return fbb

    @classmethod
    def from_path(cls, path):
        return cls(open(path, "rb"), os.path.getsize(path))

    @property
    def name(self):
        return self.fp.name

    @property
    def chunksize(self):
        """
        Returns a chunksize to be used when reading the data.

        Attempts to create 1024 chunks bounded by min and max chunk sizes.
        """
        return min(self._max_chunk, max(self._min_chunk, self.length // 1024))

    def write(self, data):
        written = self.fp.write(data)
        self.length += written
        return written

    def seek(self, offset):
        self.fp.seek(offset)

    def read(self, size=-1):
        return self.fp.read(size)

    def readall(self):
        pos = self.fp.tell()
        try:
            self.fp.seek(0)
            return self.fp.read()
        finally:
            self.fp.seek(pos)

    def flush(self):
        self.fp.flush()

    def __len__(self):
        return self.length

    def __str__(self):
        return f"<FileBackedBuffer {self.fp}, {self.length} bytes, {self.chunksize} chunk size>"


class FramedMessage:
    """
    FramedMessage is a container for a header and optional payload that
    encapsulates serialization for transmission across the network.

    :param msg_id: should be an integer representation of a type4 uuid
    :param header: should be a mapping
    :param payload: if set, should be a file-like object that exposes seek() and
                    read() that accepts a size argument.
    """

    __slots__ = ("msg_id", "header", "payload")

    def __init__(self, msg_id=None, header=None, payload=None):
        if msg_id is None:
            msg_id = uuid.uuid4().int
        self.msg_id = msg_id
        self.header = header
        self.payload = payload

    def __repr__(self):
        return f"FramedMessage(msg_id={self.msg_id}, header={self.header}, payload={self.payload})"

    def __iter__(self):
        header_bytes = json.dumps(self.header).encode("utf-8")
        yield Frame.wrap(
            header_bytes,
            type_=Frame.Types.HEADER if self.payload else Frame.Types.COMMAND,
            msg_id=self.msg_id,
        ).serialize()
        yield header_bytes
        if self.payload:
            yield Frame.wrap(self.payload, msg_id=self.msg_id).serialize()
            self.payload.seek(0)
            reader = functools.partial(self.payload.read, size=self.payload.chunksize)
            for chunk in iter(reader, b""):
                yield chunk

    def serialize(self):
        return b"".join(self)


class FramedBuffer:
    """
    A buffer that accumulates frames and bytes to produce a header and a
    payload.

    This buffer assumes that an entire message (denoted by msg_id) will be
    sent before another message is sent.
    """

    def __init__(self, loop=None):
        self.q = asyncio.Queue(loop=loop)
        self.header = None
        self.framebuffer = bytearray()
        self.bb = FileBackedBuffer.from_temp()
        self.current_frame = None
        self.to_read = 0

    async def put(self, data):
        if not self.to_read:
            return await self.handle_frame(data)
        await self.consume(data)

    async def handle_frame(self, data):
        try:
            self.framebuffer += data
            frame, rest = Frame.from_data(self.framebuffer)
        except struct.error:
            return  # We don't have enough data yet
        else:
            self.framebuffer = bytearray()

        if frame.type not in Frame.Types:
            raise Exception("Unknown Frame Type")

        self.current_frame = frame
        self.to_read = self.current_frame.length
        await self.consume(rest)

    async def consume(self, data):
        data, rest = data[: self.to_read], data[self.to_read :]
        self.to_read -= self.bb.write(data)
        if self.to_read == 0:
            await self.finish()
        if rest:
            await self.handle_frame(rest)

    async def finish(self):
        if self.current_frame.type == Frame.Types.HEADER:
            self.bb.seek(0)
            self.header = json.load(self.bb)
        elif self.current_frame.type == Frame.Types.PAYLOAD:
            await self.q.put(
                FramedMessage(self.current_frame.msg_id, header=self.header, payload=self.bb)
            )
            self.header = None
        elif self.current_frame.type == Frame.Types.COMMAND:
            self.bb.seek(0)
            await self.q.put(
                FramedMessage(msg_id=self.current_frame.msg_id, header=json.load(self.bb))
            )
        else:
            raise Exception("Unknown Frame Type")
        self.to_read = 0
        self.bb = FileBackedBuffer.from_temp()

    async def get(self, timeout=None):
        return await asyncio.wait_for(self.q.get(), timeout)

    def get_nowait(self):
        return self.q.get_nowait()
