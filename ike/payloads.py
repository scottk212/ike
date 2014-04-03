# -*- coding: utf-8 -*-
#
# Copyright © 2013-2014 Kimmo Parviainen-Jalanko.
#
from enum import IntEnum
from functools import reduce
import ipaddress
import logging
import operator
import os
import struct
import binascii

from . import const
from ike.util import pubkey
from ike.util.prf import prf
from .proposal import Proposal
from .util.conv import to_bytes

PRIVATE_KEY_PEM = 'tests/private_key.pem'

__author__ = 'kimvais'

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class Type(IntEnum):
    SA = 33
    KE = 34
    IDi = 35
    IDr = 36
    CERT = 37
    CERTREQ = 38
    AUTH = 39
    Nonce = 40
    Ni = 40
    Nr = 40
    Notify = 41
    TSi = 44
    TSr = 45
    Encrypted = 46
    CP = 47
    EAP = 48


class IkePayload(object):
    _type = None

    def __init__(self, data=None, next_payload=None, critical=False):
        self._type = Type[self.__class__.__name__]
        if data is not None:
            self.next_payload, self.flags, self.length = const.PAYLOAD_HEADER.unpack(
                data[:const.PAYLOAD_HEADER.size])
            self.parse(data[const.PAYLOAD_HEADER.size:])
        else:
            self.next_payload = const.PAYLOAD_TYPES[next_payload]
            self.length = 0
            self._data = bytearray()
            if critical:
                self.flags = 0b10000000
            else:
                self.flags = 0

    @property
    def header(self):
        return bytearray(const.PAYLOAD_HEADER.pack(self.next_payload,
                                                   self.flags,
                                                   self.length))

    def __bytes__(self):
        return bytes(self.header + self._data)

    def __unicode__(self):
        return "IKE Payload {0} [{1}]".format(self.__class__.__name__,
                                              self.length)

    def __repr__(self):
        return '<{0} at {1}>'.format(self.__unicode__(), hex(id(self)))

    def parse(self, data):
        self._data = data


class SA(IkePayload):
    def __init__(self, data=None, proposals=None, next_payload=None,
                 critical=False):
        super(SA, self).__init__(data, next_payload, critical)
        if data is not None:
            self.parse(data)
        elif proposals is None:
            self.proposals = [
                Proposal(None, 1, const.ProtocolID.IKE, transforms=[
                    ('ENCR_CAMELLIA_CBC', 256),
                    ('PRF_HMAC_SHA2_256',),
                    ('AUTH_HMAC_SHA2_256_128',),
                    ('DH_GROUP_14',)
                ]),
                Proposal(None, 2, const.ProtocolID.ESP, transforms=[
                    ('ENCR_CAMELLIA_CBC', 256),
                    ('ESN', ),
                    ('AUTH_HMAC_SHA2_256_128',)
                ])
            ]
        else:
            self.proposals = proposals
        self.spi = self.proposals[0].spi

    def __bytes__(self):
        ret = list()
        self.proposals[-1].last = True
        ret.extend(proposal.data for proposal in self.proposals)
        self.length = 4 + sum((len(x) for x in ret))
        ret.insert(0, self.header)
        return bytes(reduce(operator.add, ret))

    def parse(self, data):
        self.proposals = list()
        last = False
        while not last:
            proposal = Proposal(data=data)
            self.proposals.append(proposal)
            last = proposal.last
            data = data[proposal.len:]


class KE(IkePayload):
    def parse(self, data):
        self.group, _ = struct.unpack('!2H', data[4:8])
        self.kex_data = data[const.PAYLOAD_HEADER.size + 4:self.length]
        logger.debug("group {}".format(self.group))
        logger.debug('KEX data: {}'.format(binascii.hexlify(self.kex_data)))

    def __init__(self, data=None, next_payload=None, critical=False,
                 group=14, diffie_hellman=None):
        super(KE, self).__init__(data, next_payload, critical)
        if data is not None:
            self.parse(data)
        else:
            self.kex_data = to_bytes(diffie_hellman.public_key)
            self._data = struct.pack('!2H', group, 0) + self.kex_data
            self.length = const.PAYLOAD_HEADER.size + len(self._data)


class Nonce(IkePayload):
    def parse(self, data):
        self._data = data[const.PAYLOAD_HEADER.size:self.length]

    def __init__(self, data=None, next_payload=None, critical=False,
                 nonce=None):
        super(Nonce, self).__init__(data, next_payload, critical)
        if data is not None:
            self.parse(data)
        else:
            if nonce:
                self._data = nonce
            else:
                self._data = os.urandom(32)
            self.length = const.PAYLOAD_HEADER.size + len(self._data)


class Notify(IkePayload):
    def parse(self, data):
        self._data = data[4:self.length]
        self.protocol_id, self.spi_size, message_type = struct.unpack(
            '!2BH', data[:4])
        self.spi = data[4:4 + self.spi_size]
        self.message_type = const.MessageType(message_type)
        if self.message_type < 2 ** 14:
            self.level = logging.ERROR
        else:
            self.level = logging.INFO
        logger.log(self.level, self.__unicode__())
        self.notification_data = data[4 + self.spi_size:self.length]

    def __unicode__(self):
        if self.protocol_id:
            return 'Notify payload for {0}: {1!r} (spi {2} [{3}]) [{4}]'.format(
                const.ProtocolID(self.protocol_id),
                self.message_type, binascii.hexlify(self.spi),
                self.spi_size, self.length)
        else:
            return 'Notify payload {0!r} [{1}]'.format(self.message_type, self.length)


class _TS(IkePayload):
    """
    Single IPv4 address:port
    """
    def __init__(self, addr=None, data=None, next_payload=None, critical=False):
        assert addr or data
        super().__init__(data, next_payload, critical)
        if addr:
            ip = int(ipaddress.IPv4Address(addr[0]))
            port = addr[1]

            # Generate traffic selector
            selector = struct.pack("!2BH2H2I", 7, 0, 16, port, port, ip, ip)
            self._data = struct.pack("!B3x", 1) + selector # just a single TS
            self.length = len(self._data) + 4

class TSi(_TS):
    pass


class TSr(_TS):
    pass


class IDi(IkePayload):
    def __init__(self, data=None, next_payload=None, critical=False):
        super().__init__(data, next_payload, critical)
        EMAIL = b'test@77.fi'
        self.length = 8 + len(EMAIL)
        self._data = struct.pack("!B3x", 3) + EMAIL  # ID Type (RFC822 address) + reserved


class IDr(IkePayload):
    pass


class AUTH(IkePayload):
    def __init__(self, signed_octets=None, data=None, next_payload=None, critical=False):
        assert signed_octets or data
        super().__init__(data, next_payload, critical)
        if not data:
            # Generate auth payload

            # authentication_type = const.AuthenticationType.PSK
            authentication_type = const.AuthenticationType.RSA

            if authentication_type == const.AuthenticationType.PSK:
                PSK = b"foo"
                authentication_data = prf(prf(PSK, b"Key Pad for IKEv2"), signed_octets)[:const.AUTH_MAC_SIZE]
            elif authentication_type == const.AuthenticationType.RSA:
                # XXX: StrongSwan can not verify SHA-256 signature, so we have to use SHA-1
                authentication_data = pubkey.sign(signed_octets, PRIVATE_KEY_PEM, hash_alg='SHA-1')
            else:
                authentication_data = b''
                raise AssertionError("Unsupported authentication method")
            self.length = 8 + len(authentication_data)
            self._data = struct.pack("!B3x", authentication_type) + authentication_data


# Register payloads in order to be used for get_by_type()
_payload_classes = IkePayload.__subclasses__() + _TS.__subclasses__()
_payload_map = {x.__name__: x for x in _payload_classes if not x.__name__.startswith('_')}


def get_by_type(payload_type):
    """
    Returns an IkePayload (sub)class based on the RFC5996 payload_type
    """
    return _payload_map.get(Type(payload_type).name, IkePayload)

