import unittest

from douyin_downloader import core


class DouyinCoreTests(unittest.TestCase):
    def test_extracts_share_url_from_mixed_clipboard_text(self):
        text = "0.07 hBt:/ 07/25 https://v.douyin.com/MzR141DOMWE/ 复制此链接"

        self.assertEqual(
            core.extract_url(text),
            "https://v.douyin.com/MzR141DOMWE/",
        )

    def test_extracts_video_id_from_redirect_location(self):
        location = "https://www.douyin.com/video/7613351087721991443?previous_page=web_code_link"

        self.assertEqual(core.extract_aweme_id(location), "7613351087721991443")

    def test_extracts_play_url_from_share_page(self):
        html = r'''
        {"video":{"play_addr":{"uri":"v0d00fg10000d6k0qe7og65ssaq4mi60",
        "url_list":["https:\u002F\u002Faweme.snssdk.com\u002Faweme\u002Fv1\u002Fplaywm\u002F?video_id=v0d00fg10000d6k0qe7og65ssaq4mi60&ratio=720p&line=0"]}}}
        '''

        self.assertEqual(
            core.extract_play_url(html),
            "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=v0d00fg10000d6k0qe7og65ssaq4mi60&ratio=720p&line=0",
        )

    def test_builds_safe_filename_from_title_and_id(self):
        name = core.build_filename("亲测有效无广！15s搞懂AI工具如何选 # ai / demo", "7613351087721991443")

        self.assertEqual(name, "亲测有效无广_15s搞懂AI工具如何选_ai_demo_7613351087721991443.mp4")


if __name__ == "__main__":
    unittest.main()
