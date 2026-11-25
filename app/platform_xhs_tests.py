import contextlib
import json
import html
import re
import subprocess
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import platform_xhs


class XHSDistributionTests(unittest.TestCase):
    @unittest.skipUnless(Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome').is_file(), '本机无 Chrome')
    def test_gallery_input_replacement_and_rich_paragraphs_in_chrome(self):
        from publish_checks import FORM_STATE,validate_state
        scripts=Path(__file__).parent/'vendor/xiaohongshu-skills/scripts'
        with patch.object(sys,'path',[str(scripts),*sys.path]):
            from xhs.publish import _image_input_selector
        page=Mock()
        _image_input_selector(page,True)
        first=page.evaluate.call_args.args[0]
        _image_input_selector(page,False)
        next_input=page.evaluate.call_args.args[0]
        fixture=Path(self.temp.name)/'editor.html'
        fixture.write_text("""<!doctype html><meta charset="utf-8">
            <input id="initial" type="file" class="upload-input" accept="image/*">
            <style>.content-hide{display:none}</style>
            <div role="combobox"><span class="select-placeholder">选择合集</span></div>
            <div class="tiptap ProseMirror" contenteditable="true"><p>第一段正文</p><p>第二段正文</p>
              <p><a class="tiptap-topic" data-topic="摄影">#摄影<span class="content-hide">[话题]#</span></a>&nbsp;</p>
              <p class="is-empty"><br class="ProseMirror-trailingBreak"></p></div><pre id="out"></pre>
            <script>const first=document.querySelector(FIRST).id;
            document.getElementById('initial').remove();
            document.body.insertAdjacentHTML('beforeend','<input id="cover" type="file" accept=".jpg,.jpeg,.png,.webp"><input id="gallery" type="file" accept=".jpg,.jpeg,.png,.webp" multiple>');
            const gallery=document.querySelector(NEXT).id;
            document.getElementById('gallery').remove();
            const missing=NEXT;
            const official=FORM;
            const editor=document.querySelector('.tiptap');
            editor.className='ql-editor';
            editor.innerHTML='<p>首行</p><p><br></p><p>三 行<br>四行</p><p><span class="ql-topic">#&nbsp;摄影[话题]#</span></p>';
            const state=FORM;
            document.getElementById('out').textContent=JSON.stringify({first,gallery,missing,state,official});
            </script>""".replace('FIRST',first).replace('NEXT',next_input).replace('FORM',FORM_STATE),encoding='utf-8')
        chrome='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
        output=subprocess.run([chrome,'--headless','--disable-background-networking','--no-first-run',
                               '--user-data-dir='+self.temp.name+'/chrome','--dump-dom',fixture.as_uri()],
                              text=True,capture_output=True,timeout=30,check=True).stdout
        result=json.loads(html.unescape(re.search(r'<pre id="out">(.*?)</pre>',output,re.S)[1]))
        self.assertEqual((result['first'],result['gallery'],result['missing']),('initial','gallery',''))
        official={**result['official'],'original':False,'visibility':'公开可见'}
        native_draft={'content':'第一段正文\n第二段正文','original':False,'tags':['摄影'],'album':''}
        validate_state(official,native_draft)
        self.assertEqual(official['topics'],['摄影'])
        with self.assertRaisesRegex(ValueError,'正文'):
            validate_state(official,{**native_draft,'content':'第一段正文第二段正文'})
        state={**result['state'],'original':False,'visibility':'公开可见'}
        draft={'content':'首行\n\n三 行\n四行','original':False,'tags':['摄影']}
        validate_state(state,draft)
        with self.assertRaisesRegex(ValueError,'正文'):
            validate_state(state,{**draft,'content':'首行\n三 行\n四行'})

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
        self.app.prepare_publish.return_value={'history_id':7,'verification_payload':{**self.payload,'native_image_refs':['preview-1']}}
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

    def test_frozen_batch_paths_cannot_bypass_previous_submission(self):
        self.payload['source_images'] = ['old/source.jpg']
        self.payload['frozen_images'] = [{'path':'old/upload.jpg'}]
        with patch('publish_checks.verify'):platform_xhs.submit(self.app,self.prepare())
        other = self.image.with_name('second.jpg')
        other.write_bytes(self.image.read_bytes())
        self.payload.update(images=[str(other)],source_images=['new/source.jpg'],frozen_images=[{'path':'new/upload.jpg'}])
        with self.assertRaisesRegex(ValueError,'已经尝试发布'):self.prepare()

    def test_legacy_unknown_receipt_migrates_without_reading_old_paths(self):
        with self.app.db_connection() as db:
            db.execute("""CREATE TABLE platform_xhs_receipts (id TEXT PRIMARY KEY, payload TEXT, prepared TEXT,
                binding TEXT, fingerprint TEXT, status TEXT, created_at REAL, message TEXT DEFAULT '')""")
            old_payload = {**self.payload,'media_type':'image','images':['/removed/task.jpg']}
            binding = {'user_id':'account-one','files':platform_xhs.file_stats([str(self.image)])}
            db.execute("INSERT INTO platform_xhs_receipts VALUES (?,?,?,?,?,'unknown',123,'')",
                       ('old',json.dumps(old_payload),'{}',json.dumps(binding),'old-key'))
        with self.assertRaisesRegex(ValueError,'已经尝试发布'):self.prepare()
        with self.app.db_connection() as db:
            row = db.execute("SELECT fingerprint,status,attempted_at FROM platform_xhs_receipts").fetchone()
        self.assertNotEqual(row[0],'old-key')
        self.assertEqual(row[1:],('unknown',None))

    def test_images_body_and_each_official_setting_must_match(self):
        from publish_checks import validate_state
        draft = dict(title='标题',content='第一段\n第二段',tags=['摄影'],images=['a','b'],
                     native_image_refs=['ref-a','ref-b'],album='摄影',location='',original=False)
        state = {**draft,'content':'第一段\n第二段 #摄影','image_count':2,'visibility':'公开可见','topics':['摄影']}
        validate_state(state,draft)
        for change in [{'content':'第一段第二段 #摄影'}, {'native_image_refs':['ref-b','ref-a']},
                       {'native_image_refs':['ref-a','ref-c']}, {'location':'北京'},
                       {'album':'','location':'摄影'}, {'album':None}]:
            with self.subTest(change=change),self.assertRaises(ValueError):
                validate_state({**state,**change},draft)


if __name__=='__main__':unittest.main()
