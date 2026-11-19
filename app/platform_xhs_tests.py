import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import platform_xhs


class XHSDistributionTests(unittest.TestCase):
    def test_native_source_details_and_modal_must_match(self):
        from publish_checks import validate_state
        draft = dict(title='标题', content='正文', visibility='公开可见', original=False,
                     declaration='内容来源声明：来源转载', repost_source='测试媒体')
        state = {**draft, 'declaration':'来源转载', 'source_note':'信息转载于测试媒体'}
        validate_state(state, draft)
        for change in [{'source_note':'信息转载于其他媒体'}, {'source_dialog':True}]:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, '来源声明'):
                validate_state({**state, **change}, draft)
        self_shot = {**draft, 'declaration':'内容来源声明：自主拍摄', 'shooting_date':'2026-10-07', 'shooting_location':'北京市'}
        validate_state({**state, 'declaration':'自主拍摄', 'source_note':'拍摄于2026-10-07，北京市'}, self_shot)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        path=Path(self.temp.name);self.image=path/'test.jpg';self.image.write_bytes(b'test')
        @contextlib.contextmanager
        def connection():
            db=sqlite3.connect(path/'test.db')
            try:yield db;db.commit()
            finally:db.close()
        self.app=Mock();self.app.db_connection=connection
        self.app.require_active_profile.return_value='Profile1'
        self.app.account_identity.return_value={'user_id':'account-one'}
        self.app.now_iso.return_value='2026-11-18T02:00'
        self.payload={'profile_dir':'Profile1','title':'测试标题','content':'测试正文','images':[str(self.image)],'tags':[]}
        self.app.prepare_publish.return_value={'history_id':7,'verification_payload':self.payload}
        self.app.manual_pending.return_value={'history_id':7}
        self.app.click_publish.return_value={'ok':True}

    def prepare(self):
        return platform_xhs.prepare(self.app,{'media_type':'image','payload':self.payload})

    def test_prepare_never_submits(self):
        self.assertTrue(self.prepare()['verified'])
        self.app.click_publish.assert_not_called()
        self.app.verify_prepared_publish.assert_called_once()

    def test_video_requires_a_video_asset(self):
        with self.assertRaisesRegex(ValueError,'视频或图片素材'):
            platform_xhs.prepare(self.app,{'media_type':'video','payload':self.payload})
        self.app.prepare_publish.assert_not_called()

    def test_final_check_does_not_append_mentions_again(self):
        r=self.prepare()
        with patch('publish_checks.verify') as verify:
            result=platform_xhs.submit(self.app,r)
            self.assertEqual(result['status'],'submitted')
            self.assertFalse(verify.call_args.kwargs['apply_mentions'])
        self.app.clear_manual_pending.assert_called_once_with(7,'Profile1')

    def test_uncertain_result_cannot_be_retried(self):
        r=self.prepare();self.app.click_publish.side_effect=RuntimeError('lost response')
        with patch('publish_checks.verify'):
            self.assertEqual(platform_xhs.submit(self.app,r)['status'],'unknown')
            with self.assertRaisesRegex(ValueError,'已失效或已提交'):platform_xhs.submit(self.app,r)
        self.app.click_publish.assert_called_once()
        self.app.clear_manual_pending.assert_not_called()

    def test_changed_media_blocks_before_click(self):
        r=self.prepare();self.image.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'素材已变化'):platform_xhs.submit(self.app,r)
        self.app.click_publish.assert_not_called()

    def test_previous_submission_blocks_new_batch(self):
        r=self.prepare()
        with patch('publish_checks.verify'):platform_xhs.submit(self.app,r)
        with self.assertRaisesRegex(ValueError,'已经尝试发布'):self.prepare()


if __name__=='__main__':unittest.main()
