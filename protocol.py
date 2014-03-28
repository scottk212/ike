# -*- coding: utf-8 -*-
#
# Copyright © 2014 Kimmo Parviainen-Jalanko.
#

from functools import reduce
from hmac import HMAC
import logging
import operator
import os
from hashlib import sha256
from struct import Struct, pack, unpack
import binascii

from util.dump import dump
from util.cipher import Camellia
import payloads
import const
import proposal
from util.conv import to_bytes
from util.dh import DiffieHellman
from util.prf import prf, prfplus


IKE_HEADER = Struct("!2Q4B2I")
PAYLOAD = Struct("!2BH")
MACLEN = 16

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class IKE(object):
    def __init__(self, dh_group=14, nonce_len=32):
        self.iSPI = 0
        self.rSPI = 0
        self.diffie_hellman = DiffieHellman(dh_group)
        self.Ni = os.urandom(nonce_len)
        self.packets = list()

    def init(self):
        packet = Packet()
        self.packets.append(packet)
        packet.payloads.append(payloads.SA(next_payload="KE"))
        packet.payloads.append(payloads.KE(next_payload="Ni",
                                           diffie_hellman=self.diffie_hellman))
        packet.payloads.append(payloads.Nonce(nonce=self.Ni))
        self.iSPI = packet.payloads[0].spi
        packet.data = reduce(operator.add, (x.data for x in packet.payloads))
        packet.header = bytearray(const.IKE_HEADER.pack(
            self.iSPI,
            self.rSPI,
            packet.payloads[0]._type,
            const.IKE_VERSION,
            const.IKE_SA_INIT,
            const.IKE_HDR_FLAGS['I'],
            0,
            (len(packet.data) + const.IKE_HEADER.size)
        ))
        return packet.header + packet.data

    def auth(self):
        # self.iSPI = self.packets[0].iSPI
        # self.rSPI = self.packets[-1].rSPI
        self.packets.append(Packet())
        return self.ike_auth(self)


    def ike_auth(self, packet):

        plain = bytearray()
        # Add IDi (35)
        #
        EMAIL = b"k@77.fi"
        plain += PAYLOAD.pack(39, 0, 8 + len(EMAIL))
        plain += pack("!B3x", 3)  # ID Type (RFC822 address) + reserved
        plain += EMAIL

        # Add AUTH (39)
        #
        PSK = b"foo"

        IDi = bytes(plain)[PAYLOAD.size:]

        plain += PAYLOAD.pack(33, 0, 8 + const.AUTH_MAC_SIZE)  # prf always returns 20 bytes
        plain += pack("!B3x", 2)  # AUTH Type (psk) + reserved
        #logger.debug "%r\n%r" % (IDi, plain)

        # XXX: This should be parsed when receiving.
        # find Nr
        for p in self.packets[1].payloads:
            if p._type == 40:
                self.Nr = p._data
                logger.debug(u"Responder nonce {}".format(binascii.hexlify(self.Nr)))
            elif p._type == 34:
                int_from_bytes = int.from_bytes(p.kex_data, 'big')
                #int_from_bytes = int(str(p.kex_data).encode('hex'), 16)
                self.diffie_hellman.derivate(int_from_bytes)

        logger.debug('Nonce I: {}\nNonce R: {}'.format(binascii.hexlify(self.Ni), binascii.hexlify(self.Nr)))
        logger.debug('DH shared secret: {}'.format(binascii.hexlify(self.diffie_hellman.shared_secret)))

        SKEYSEED = prf(self.Ni + self.Nr, self.diffie_hellman.shared_secret)

        logger.debug(u"SKEYSEED is: {0!r:s}\n".format(binascii.hexlify(SKEYSEED)))

        keymat = prfplus(SKEYSEED, (self.Ni + self.Nr +
                                    to_bytes(self.iSPI) + to_bytes(self.rSPI)),
                         32 * 7)
        #3 * 32 + 2 * 32 + 2 * 32)

        logger.debug("Got %d bytes of key material" % len(keymat))
        # get keys from material
        ( self.SK_d,
          self.SK_ai,
          self.SK_ar,
          self.SK_ei,
          self.SK_er,
          self.SK_pi,
          self.SK_pr ) = unpack("32s" * 7, keymat)

        # Generate auth payload

        message1 = bytearray(self.packets[0].data)
        logger.debug("Original packet len: %d" % len(message1))
        signed = message1 + self.Nr + prf(self.SK_pi, IDi)
        plain += prf(prf(PSK, b"Key Pad for IKEv2"), signed)[:const.AUTH_MAC_SIZE]  # AUTH data

        # Add SA (33)
        #
        self.esp_SPIout = os.urandom(4)
        prop = proposal.Proposal(protocol='ESP', spi=self.esp_SPIout, last=True, transforms=[
            ('ENCR_CAMELLIA_CBC', 256), ('AUTH_HMAC_SHA2_256_128',)])
        # ('ENCR_CAMELLIA_CBC', 256), ('ESN',), ('AUTH_HMAC_SHA2_256_128',)])
        plain += PAYLOAD.pack(44, 0, len(prop.data) + 4) + prop.data

        # Generate traffic selectors
        ts = pack("!2BH2H2I", 7, 0, 16, 0, 0, 0, 0)  # Propose everything

        # Add TSi (44)
        plain += PAYLOAD.pack(45, 0, 8 + len(ts))  # 12 = Payload header, + B3x + TS header
        plain += pack("!B3x", 1) + ts  # just a single TS

        # Add TSr (45)
        plain += PAYLOAD.pack(0, 0, 8 + len(ts))
        plain += pack("!B3x", 1) + ts  # just a single TS

        # Encrypt and hash
        iv = os.urandom(16)

        ikecrypto = Camellia(self.SK_ei, iv)

        logger.debug('IV: {}'.format(binascii.hexlify(iv)))
        logger.debug('IKE packet in plain: {}'.format(binascii.hexlify(plain)))
        # Encrypt
        ciphertext = ikecrypto.encrypt(plain)
        payload_len = PAYLOAD.size + len(iv) + len(ciphertext) + MACLEN
        enc_payload = PAYLOAD.pack(35, 0, payload_len) + iv + ciphertext

        # IKE Header
        data = IKE_HEADER.pack(
            self.iSPI,
            self.rSPI,
            46,  # first payload (encrypted)
            const.IKE_VERSION,
            35,  # exchange_type (AUTH)
            const.IKE_HDR_FLAGS['I'],
            1,  # message_id
            len(enc_payload) + IKE_HEADER.size + MACLEN
        ) + enc_payload

        logger.debug(dump(data))
        # Sign
        ikehash = HMAC(self.SK_ai, digestmod=sha256)
        ikehash.update(data)
        mac = ikehash.digest()[:MACLEN]
        logger.debug("HMAC: {}".format(binascii.hexlify(mac)))
        return data + mac


class Packet(object):
    def __init__(self, data=None, exchange_type=None):
        self.payloads = list()
        self.data = ''
        self.iSPI = self.rSPI = 0
        self.length = 0
        self.header = ''


def parse_packet(data, ike=None):
    raw_data = data
    packet = Packet()
    data = bytearray(raw_data)
    packet.header = data[0:const.IKE_HEADER.size]
    (packet.iSPI, packet.rSPI, next_payload, packet.version, packet.exchange_type, packet.flags,
     packet.message_id, packet.length) = const.IKE_HEADER.unpack(packet.header)
    remainder = data[const.IKE_HEADER.size:]
    logger.debug("next payload: {}".format(next_payload))
    if next_payload == 46:
        next_payload, is_critical, payload_len = PAYLOAD.unpack(remainder[:PAYLOAD.size])
        try:
            iv = remainder[PAYLOAD.size:PAYLOAD.size + 16]
            ciphertext = remainder[PAYLOAD.size + 16:payload_len]  # HMAC size
            hmac_theirs = remainder[-MACLEN:]
        except IndexError:
            logger.critical('Malformed packet')

        logger.debug('IV: {}'.format(dump(iv)))
        logger.debug('CIPERTEXT: {}'.format(dump(ciphertext)))
        hmac = HMAC(ike.SK_ar, digestmod=sha256)
        hmac.update(raw_data[:-MACLEN])
        hmac_ours = hmac.digest()[:MACLEN]
        logger.debug('HMAC verify (ours){} (theirs){}'.format(
            binascii.hexlify(hmac_ours), binascii.hexlify(hmac_theirs)))
        assert hmac_ours == hmac_theirs  # TODO: raise IkeError
        # TODO: Decrypt
    while next_payload:
        logger.debug('Next payload: {0}'.format(next_payload))
        logger.debug('{0} bytes remaining'.format(len(remainder)))
        try:
            payload = payloads.BY_TYPE[next_payload](data=remainder)
        except KeyError as e:
            logger.error("Unidentified payload {}".format(e))
            payload = payloads.IkePayload(data=remainder)
        packet.payloads.append(payload)
        logger.debug('Payloads: {0!r}'.format(packet.payloads))
        next_payload = payload.next_payload
        remainder = remainder[payload.length:]
    logger.debug("Packed parsed successfully")
    return packet


