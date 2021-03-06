# -*- coding: utf-8 -*-
#
# Copyright (C) 2014-2016 Alexander Shorin
# All rights reserved.
#
# This software is licensed as described in the file LICENSE, which
# you should have received as part of this distribution.
#

import asyncio
import json

from aiohttp.helpers import parse_mimetype
from .hdrs import CONTENT_TYPE


__all__ = (
    'Feed',
    'JsonFeed',
    'ViewFeed',
    'ChangesFeed',
    'LongPollChangesFeed',
    'ContinuousChangesFeed',
    'EventSourceFeed',
    'EventSourceChangesFeed'
)


class Feed(object):
    """Wrapper over :class:`HttpResponse` content to stream continuous response
    by emitted chunks."""

    #: Limits amount of items feed would fetch and keep for further iteration.
    buffer_size = 0
    _ignore_heartbeats = True

    def __init__(self, resp, *, loop=None, buffer_size=0):
        self._active = True
        self._exc = None
        self._queue = asyncio.Queue(maxsize=buffer_size or self.buffer_size,
                                    loop=loop)
        self._resp = resp

        ctype = resp.headers.get(CONTENT_TYPE, '').lower()
        mimetype = parse_mimetype(ctype)
        self._encoding = mimetype.parameters.get('charset', 'utf-8')  # pylint: disable=E1101

        asyncio.Task(self._loop(), loop=loop)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @asyncio.coroutine
    def _loop(self):
        try:
            while not self._resp.content.at_eof() and self._active:
                chunk = yield from self._resp.content.readline()
                if not chunk:
                    continue
                if chunk == b'\n' and self._ignore_heartbeats:
                    continue
                yield from self._queue.put(chunk)
        except Exception as exc:
            self._exc = exc
            self.close()
        else:
            self.close()

    @asyncio.coroutine
    def next(self):
        """Emits the next response chunk or ``None`` is feed is empty.

        :rtype: bytearray
        """
        if not self.is_active():
            if self._exc is not None:
                raise self._exc from None  # pylint: disable=raising-bad-type
            return None
        chunk = yield from self._queue.get()
        if chunk is None:
            # in case of race condition, raising an error should have more
            # priority then returning stop signal
            if self._exc is not None:
                raise self._exc from None  # pylint: disable=raising-bad-type
        return chunk

    def is_active(self):
        """Checks if the feed is still able to emit any data.

        :rtype: bool
        """
        return self._active or not self._queue.empty()

    def close(self):
        """Closes feed and the related request connection. Closing feed doesnt
        means that all
        """
        self._active = False
        self._resp.close()
        # put stop signal into queue to break waiting loop on queue.get()
        self._queue.put_nowait(None)


class JsonFeed(Feed):
    """As :class:`Feed`, but for chunked JSON response. Assumes that each
    received chunk is valid JSON object and decodes them before emit."""

    @asyncio.coroutine
    def next(self):
        """Decodes feed chunk with JSON before emit it.

        :rtype: dict
        """
        chunk = yield from super().next()
        if chunk is not None:
            return json.loads(chunk.decode(self._encoding))


class ViewFeed(Feed):
    """Like :class:`JsonFeed`, but uses CouchDB view response specifics."""

    _total_rows = None
    _offset = None
    _update_seq = None

    @asyncio.coroutine
    def next(self):
        """Emits view result row.

        :rtype: dict
        """
        chunk = yield from super().next()
        if chunk is None:
            return chunk
        chunk = chunk.decode(self._encoding).strip('\r\n,')
        if "total_rows" in chunk:
            # couchdb 1.x (and 2.x?)
            if chunk.startswith('{'):
                chunk += ']}'
            # couchbase sync gateway 1.x
            elif chunk.endswith('}'):
                chunk = '{' + chunk
            event = json.loads(chunk)
            self._total_rows = event['total_rows']
            self._offset = event.get('offset')
            return (yield from self.next())
        elif chunk.startswith(('{"rows"', ']')):
            return (yield from self.next())
        elif not chunk:
            return (yield from self.next())
        else:
            return json.loads(chunk)

    @property
    def offset(self):
        """Returns view results offset."""
        return self._offset

    @property
    def total_rows(self):
        """Returns total rows in view."""
        return self._total_rows

    @property
    def update_seq(self):
        """Returns update sequence for a view."""
        return self._update_seq


class EventSourceFeed(Feed):
    """Handles `EventSource`_ response following the W3.org spec with single
    exception: it expects field `data` to contain valid JSON value.

    .. _EventSource: http://www.w3.org/TR/eventsource/
    """

    _ignore_heartbeats = False

    @asyncio.coroutine
    def next(self):
        """Emits decoded EventSource event.

        :rtype: dict
        """
        lines = []
        while True:
            chunk = (yield from super().next())
            if chunk is None:
                if lines:
                    break
                return
            if chunk == b'\n':
                if lines:
                    break
                continue
            chunk = chunk.decode(self._encoding).strip()
            lines.append(chunk)
        event = {}
        data = event['data'] = []
        for line in lines:
            if not line:
                break
            if line.startswith(':'):
                # If the line starts with a U+003A COLON character (:)
                # Ignore the line.
                continue
            if ':' not in line:
                # Otherwise, the string is not empty but does not contain
                # a U+003A COLON character (:)
                # Process the field using the steps described below, using
                # the whole line as the field name, and the empty string as
                # the field value.
                field, value = line, ''
            else:
                # If the line contains a U+003A COLON character (:)
                # Collect the characters on the line before the first
                # U+003A COLON character (:), and let field be that string.
                #
                # Collect the characters on the line after the first U+003A
                # COLON character (:), and let value be that string.
                # If value starts with a U+0020 SPACE character, remove it
                # from value.
                #
                # Process the field using the steps described below, using
                # field as the field name and value as the field value.
                field, value = line.split(':', 1)
                if value.startswith(' '):
                    value = value[1:]

            if field in ('id', 'event'):
                event[field] = value
            elif field == 'data':
                # If the field name is "data":
                # Append the field value to the data buffer,
                # then append a single U+000A LINE FEED (LF) character
                # to the data buffer.
                data.append(value)
                data.append('\n')
            elif field == 'retry':
                # If the field name is "retry":
                # If the field value consists of only ASCII digits,
                # then interpret the field value as an integer in base ten.
                event[field] = int(value)
            else:
                # Otherwise: The field is ignored.
                continue  # pragma: no cover
        data = ''.join(data).strip()
        event['data'] = json.loads(data) if data else None
        return event


class ChangesFeed(Feed):
    """Processes database changes feed."""

    _last_seq = None

    @asyncio.coroutine
    def next(self):
        """Emits the next event from changes feed.

        :rtype: dict
        """
        chunk = yield from super().next()
        if chunk is None:
            return chunk
        if chunk.startswith((b'{"results"', b'],\n')):
            return (yield from self.next())
        if chunk == b',\r\n':
            return (yield from self.next())
        if chunk.startswith(b'"last_seq":'):
            chunk = b'{' + chunk
        try:
            event = json.loads(chunk.strip(b',\r\n').decode(self._encoding))
        except:
            print('>>>', chunk)
            raise
        if 'last_seq' in event:
            self._last_seq = event['last_seq']
            return (yield from self.next())
        self._last_seq = event['seq']
        return event

    @property
    def last_seq(self):
        """Returns last emitted sequence number.

        :rtype: int
        """
        return self._last_seq


class LongPollChangesFeed(ChangesFeed):
    """Processes long polling database changes feed."""


class ContinuousChangesFeed(ChangesFeed, JsonFeed):
    """Processes continuous database changes feed."""

    @asyncio.coroutine
    def next(self):
        """Emits the next event from changes feed.

        :rtype: dict
        """
        event = yield from JsonFeed.next(self)
        if event is None:
            return None
        if 'last_seq' in event:
            self._last_seq = event['last_seq']
            return (yield from self.next())
        self._last_seq = event['seq']
        return event


class EventSourceChangesFeed(ChangesFeed, EventSourceFeed):
    """Process event source database changes feed.
    Similar to :class:`EventSourceFeed`, but includes specifics for changes feed
    and emits events in the same format as others :class:`ChangesFeed` does.
    """

    @asyncio.coroutine
    def next(self):
        """Emits the next event from changes feed.

        :rtype: dict
        """
        event = (yield from EventSourceFeed.next(self))
        if event is None:
            return event
        if event.get('event') == 'heartbeat':
            return (yield from self.next())
        if 'id' in event:
            self._last_seq = int(event['id'])
        return event['data']
