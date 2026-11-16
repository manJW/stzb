"""The public decoder is self-contained and preserves the wire frame formats."""
import json,struct,unittest,zlib
from unittest.mock import patch
import capture_decoder
from collector import DECODER,MessageFramer


class DecoderTests(unittest.TestCase):
    def frame(self,value,encoding,client=False):
        header=bytearray(57 if client else 13);raw=json.dumps(value).encode();header[-1]=encoding
        struct.pack_into('>I',header,44 if client else 4,92)
        if encoding==3:raw=zlib.compress(raw)
        elif encoding==5:
            key=0x98
            if client:header[55]=key
            raw=bytes(x^key for x in raw)
        struct.pack_into('>I',header,0,len(header)+len(raw)-4)
        return bytes(header)+raw

    def test_server_and_client_encodings_and_split_frames(self):
        self.assertIs(DECODER,capture_decoder)
        value=[{'battle_id':1,'attack_name':'样本'}]
        for client in (False,True):
            for encoding in (2,3,5):
                with self.subTest(client=client,encoding=encoding):
                    frame=self.frame(value,encoding,client)
                    self.assertEqual(capture_decoder.decode_body(frame,client),value)
                    received=[];errors=[]
                    parser=MessageFramer(lambda mid,data,at:received.append((mid,data)),lambda:errors.append(True),client)
                    parser.feed(frame[:9],1);parser.feed(frame[9:],2)
                    self.assertEqual(received,[(92,value)]);self.assertEqual(errors,[])

    def test_truncated_and_oversized_compression_is_rejected(self):
        raw=zlib.compress(b'{"test":1}')
        with self.assertRaises(ValueError):capture_decoder.inflate(raw[:-2])
        with patch('capture_decoder.MAX_JSON',4):
            with self.assertRaises(ValueError):capture_decoder.inflate(raw)

    def test_numeric_json_keys_and_gbk_remain_supported(self):
        self.assertEqual(capture_decoder.json_value('{123:"坐标"}'.encode('gbk')),{'123':'坐标'})
        with self.assertRaises(ValueError):capture_decoder.json_value(b'invalid payload')
