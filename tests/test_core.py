import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from twitch_downloader_gui import parse_info_output, parse_m3u8_qualities, normalize_time_arg, Settings, CliRunner

VOD_RAW = '''{"data":{"video":{"id":"123","title":"Test VOD","lengthSeconds":3661,"viewCount":1200,"createdAt":"2026-09-22T12:00:00Z","owner":{"displayName":"Streamer","login":"streamer"},"game":{"displayName":"Game"},"description":"desc"}}}\n{"data":{"video":{"moments":{"edges":[]}}}}\n#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=6500000,RESOLUTION=1920x1080,FRAME-RATE=60.000,VIDEO="1080p60"\nfoo.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=3500000,RESOLUTION=1280x720,FRAME-RATE=60.000,VIDEO="720p60"\nbar.m3u8\n'''
CLIP_RAW = '''{"data":{"clip":{"title":"Test clip","durationSeconds":32,"viewCount":50,"createdAt":"2026-09-22T12:00:00Z","broadcaster":{"displayName":"Streamer","login":"streamer"},"game":{"displayName":"Game"},"video":{"id":"123","offsetSeconds":12}}}}'''

class TestCore(unittest.TestCase):
    def test_time(self):
        self.assertEqual(normalize_time_arg("1:02:03"), "1:02:03")
        self.assertEqual(normalize_time_arg("90s"), "90s")
        self.assertEqual(normalize_time_arg("1.5m"), "90s")
    def test_vod_parse(self):
        x=parse_info_output(VOD_RAW,"123")
        self.assertEqual(x.title,"Test VOD")
        self.assertEqual(x.streamer,"Streamer")
        self.assertEqual(x.duration,3661)
        self.assertEqual(x.qualities[0].name,"1080p60")
        self.assertEqual(x.qualities[0].resolution,"1920x1080")
    def test_clip_parse(self):
        x=parse_info_output(CLIP_RAW,"abc")
        self.assertEqual(x.kind,"Клип")
        self.assertEqual(x.title,"Test clip")
        self.assertEqual(x.duration,32)
    def test_settings_roundtrip(self):
        s=Settings(cli_path="/tmp/cli",threads=8)
        self.assertEqual(s.threads,8)
    def test_builder(self):
        r=CliRunner(Settings(cli_path=sys.executable))
        cmd=r.build_video("123","out.mp4","1080p60","0:10","0:20",4,-1,"Exact","Prompt","","","")
        self.assertIn("videodownload",cmd)
        self.assertIn("--quality",cmd)
        self.assertIn("1080p60",cmd)

if __name__=="__main__": unittest.main()
