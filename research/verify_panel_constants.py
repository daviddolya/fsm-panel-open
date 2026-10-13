"""Verify one exact FSM Panel artifact without executing it or writing its data.

Only module identities, byte counts, and digests are reported. Constants may
contain credentials and private keys, so values are never printed or exported.
The algorithm is derived from native instructions, not REA native Evidence.
"""
import argparse
import collections
import hashlib
import io
import json
import pathlib
import struct
import sys
import unittest
import zlib

TARGET_SHA256 = '3731a48590e3169071b81aab9338a36b38a788133adfd43fada99ad3add1f308'
RESOURCE_SHA256 = '4905dcbc922bf9f5afe1d3aab5479bc12c483b80eec5095f8fcb7a43b276cb56'
DECODED_SHA256 = '475cedef8d71ee0d9b040afc2d8d0eb453e7dcdf64e855419881d5f97fb38ced'
RESOURCE_OFFSET = 48224628
RESOURCE_SIZE = 12437413
SUBSTITUTION_RVA = 0x2dafbb0
JUMP_TABLE_RVA = 0x2cddbcc
DELTAS_BY_RVA = {0x2cddb89:-2, 0x2cddb8e:27, 0x2cddb93:-23, 0x2cddb98:46,
                 0x2cddb9d:8, 0x2cddba2:11, 0x2cddba7:-9, 0x2cddbac:40}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def file_offset(raw, rva):
    pe = struct.unpack_from('<I',raw,0x3c)[0]
    require(raw[pe:pe+4] == b'PE\0\0','PE signature mismatch')
    require(struct.unpack_from('<H',raw,pe+24)[0] == 0x20b,'Expected PE32+')
    count = struct.unpack_from('<H',raw,pe+6)[0]
    start = pe+24+struct.unpack_from('<H',raw,pe+20)[0]
    for i in range(count):
        at = start+i*40
        virtual, size, offset = struct.unpack_from('<III',raw,at+12)
        if virtual <= rva < virtual+size:
            return offset+rva-virtual
    raise ValueError('RVA outside file-backed PE sections')


def decode(resource, substitution, deltas):
    require(len(resource) == RESOURCE_SIZE,'Resource length mismatch')
    require(len(substitution) == 256 and len(set(substitution)) == 256,'Invalid substitution permutation')
    require(len(deltas) == 8,'Invalid delta table')
    decoded = bytearray(resource[:8])+bytearray(len(resource)-24)
    state = 0
    for index in range(8,len(resource)):
        state = substitution[((index-8+state)&255)^resource[index]]
        if index >= 24:
            decoded[index-16] = state
        state = (state+deltas[index&7])&255
    crc, payload_size = struct.unpack_from('<II',decoded)
    require(len(decoded) == payload_size+8,'Decoded size mismatch')
    require(zlib.crc32(decoded[8:]) == crc,'Decoded CRC mismatch')
    return bytes(decoded)


def sections(decoded):
    cursor = 8
    result = []
    while cursor < len(decoded):
        start = cursor
        end = decoded.index(0,cursor)
        name = decoded[cursor:end].decode('utf-8')
        size = struct.unpack_from('<I',decoded,end+1)[0]
        cursor = end+5
        require(cursor+size <= len(decoded),'Section outside decoded stream')
        payload = decoded[cursor:cursor+size]
        result.append((name,payload,start,cursor))
        cursor += size
    require(cursor == len(decoded),'Incomplete section walk')
    return result


class ConstantWalker:
    """Validate the old Nuitka constant grammar without retaining string values."""
    def __init__(self, data):
        self.data = data
        self.at = 2
        self.tags = collections.Counter()
        self.blobs = []

    def take(self,n):
        require(0 <= n <= len(self.data)-self.at,'Constant exceeds section boundary')
        value = self.data[self.at:self.at+n]
        self.at += n
        return value

    def variable(self):
        value = 0
        for shift in range(0,70,7):
            part = self.take(1)[0]
            value |= (part&127)<<shift
            if part < 128:
                return value
        raise ValueError('Overlong variable integer')

    def terminated(self):
        end = self.data.index(0,self.at)
        self.take(end-self.at+1)

    def many(self,n):
        require(n <= len(self.data)-self.at,'Impossible nested count')
        for _ in range(n):
            self.one()

    def one(self):
        tag = chr(self.take(1)[0])
        self.tags[tag] += 1
        if tag in 'nptF': pass
        elif tag in 'TLSP': self.many(self.variable())
        elif tag == 'D': self.many(2*self.variable())
        elif tag in 'li': self.take(4)
        elif tag in 'qf': self.take(8)
        elif tag == 'g':
            require(self.take(1) in (b'+',b'-'),'Invalid integer sign')
            self.take(8*struct.unpack('<i',self.take(4))[0])
        elif tag in 'ZwdMQ': self.take(1)
        elif tag in 'aucOE': self.terminated()
        elif tag in 'vbB': self.take(self.variable())
        elif tag == 'X':
            size = struct.unpack('<i',self.take(4))[0]
            self.blobs.append(self.take(size))
        elif tag in ':;': self.many(3)
        elif tag in 'JAG': self.many(2)
        elif tag == 'H': self.many(1)
        elif tag == 'j': self.take(16)
        elif tag == 'C':
            flags = self.variable()
            self.one(); self.variable(); self.one(); self.variable()
            if flags&1: self.one()
            if flags&2: self.one()
            if flags&4: self.variable()
            if flags&8: self.variable()
        else:
            raise ValueError('Unknown constant tag at offset '+hex(self.at-1))

    def walk(self):
        count = struct.unpack_from('<H',self.data)[0]
        self.many(count)
        require(self.take(1) == b'.','Missing section end marker')
        require(self.at == len(self.data),'Trailing bytes in section')
        return count


def recover(path):
    raw = pathlib.Path(path).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == TARGET_SHA256,'Target SHA256 mismatch')
    resource = raw[RESOURCE_OFFSET:RESOURCE_OFFSET+RESOURCE_SIZE]
    require(hashlib.sha256(resource).hexdigest() == RESOURCE_SHA256,'Resource SHA256 mismatch')
    table_offset = file_offset(raw,SUBSTITUTION_RVA)
    substitution = raw[table_offset:table_offset+256]
    jump_table = struct.unpack_from('<8I',raw,file_offset(raw,JUMP_TABLE_RVA))
    deltas = [DELTAS_BY_RVA[rva] for rva in jump_table]
    decoded = decode(resource,substitution,deltas)
    require(hashlib.sha256(decoded).hexdigest() == DECODED_SHA256,'Decoded SHA256 mismatch')
    return resource,substitution,deltas,decoded,sections(decoded)


class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.resource,cls.substitution,cls.deltas,cls.decoded,cls.modules = recover(TARGET_PATH)

    def test_original_identity_and_decoded_crc(self):
        crc,size = struct.unpack_from('<II',self.decoded)
        self.assertEqual((crc,size),(0xa2faa7c6,12437389))
        self.assertEqual(len(self.decoded),12437397)
        self.assertNotEqual(zlib.crc32(self.resource[8:8+size]),crc)

    def test_all_458_section_counts_and_exact_extents(self):
        self.assertEqual(len(self.modules),458)
        counts = {name:ConstantWalker(payload).walk() for name,payload,_,_ in self.modules}
        self.assertEqual(counts['__main__'],9310)
        self.assertEqual(counts['__parents_main__'],9304)
        self.assertEqual(counts['sda_window'],543)
        self.assertEqual(counts['.bytecode'],388)

    def test_corrupted_payload_is_rejected(self):
        corrupted = bytearray(self.resource)
        corrupted[-1] ^= 1
        with self.assertRaisesRegex(ValueError,'CRC mismatch'):
            decode(corrupted,self.substitution,self.deltas)

    def test_truncated_resource_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'length mismatch'):
            decode(self.resource[:-1],self.substitution,self.deltas)

    def test_invalid_section_terminator_is_rejected(self):
        payload = next(p for n,p,_,_ in self.modules if n == 'sda_window')
        with self.assertRaisesRegex(ValueError,'end marker'):
            ConstantWalker(payload[:-1]+b'!').walk()

    @unittest.skipUnless(sys.version_info[:2] == (3,11),'marshal metadata requires CPython 3.11')
    def test_388_inert_marshal_records_and_12593_code_objects(self):
        import marshal
        import types
        payload = next(p for n,p,_,_ in self.modules if n == '.bytecode')
        walker = ConstantWalker(payload)
        walker.walk()
        self.assertEqual(len(walker.blobs),388)
        total = 0
        for blob in walker.blobs:
            stream = io.BytesIO(blob)
            code = marshal.load(stream)
            self.assertIsInstance(code,types.CodeType)
            self.assertEqual(stream.tell(),len(blob))
            pending = [code]
            while pending:
                current = pending.pop()
                total += 1
                pending.extend(v for v in current.co_consts if isinstance(v,types.CodeType))
        self.assertEqual(total,12593)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target',type=pathlib.Path)
    parser.add_argument('--test',action='store_true',help='Run integrity and corruption tests')
    args = parser.parse_args()
    TARGET_PATH = args.target
    if args.test:
        unittest.main(argv=[sys.argv[0]],verbosity=2)
    else:
        resource,substitution,deltas,decoded,modules = recover(args.target)
        inventory = []
        for name,payload,section_offset,data_offset in modules:
            count = ConstantWalker(payload).walk()
            inventory.append(dict(module=name,constant_count=count,bytes=len(payload),sha256=hashlib.sha256(payload).hexdigest()))
        print(json.dumps(dict(target_sha256=TARGET_SHA256,decoded_sha256=DECODED_SHA256,crc32='a2faa7c6',module_count=len(inventory),all_counts_and_extents_validated=True,modules=inventory),indent=2))
