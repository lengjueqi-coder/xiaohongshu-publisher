import contextlib
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from platform_publishing import Publisher, TZ, compare, file_stats, validate


class PlatformPublishingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file=(Path(self.tmp.name)/'image.jpg').resolve();self.file.write_bytes(b'media')
        self.payload=dict(platform='douyin',media_type='image',files=[str(self.file)],title='原始标题',content='第一行。\n第二行。',tags=['摄影'],declaration='无需添加自主声明',visibility='private',allow_download=False,publish_mode='immediate',schedule_time='')
        self.snapshot=dict(ready=True,url='https://creator.douyin.com/creator-micro/content/post/image',media_type='image',account='本人',account_id='test-account-1',fields=dict(title='原始标题',content='第一行。\n第二行。  #摄影 ',tags=['#摄影'],declaration='无需添加自主声明',collection='不选择合集',location='不显示位置',music='不添加音乐',activity='不参与活动',hotspot='不关联热点'),image_count=1,media_ready=True,media=['blob:one'],video=None,radios=['仅自己可见','不允许','不同时发布','立即发布'],errors=[])

    def test_exact_caption_and_real_topics_pass(self):
        self.assertTrue(all(x['ok'] for x in compare(self.payload,self.snapshot)))

    def test_text_hashtag_without_native_topic_fails(self):
        self.snapshot['fields']['tags']=[]
        self.assertFalse(all(x['ok'] for x in compare(self.payload,self.snapshot)))

    def test_loss_of_newline_fails(self):
        self.snapshot['fields']['content']='第一行。 第二行。 #摄影'
        self.assertFalse(all(x['ok'] for x in compare(self.payload,self.snapshot)))

    def test_visibility_reset_fails(self):
        self.snapshot['radios'][0]='公开'
        self.assertFalse(all(x['ok'] for x in compare(self.payload,self.snapshot)))

    def test_douyin_repost_requires_an_explicit_origin(self):
        self.payload['declaration'] = '内容为转载信息'
        for origin in ('', 'internal'):
            with self.subTest(origin=origin), self.assertRaisesRegex(ValueError, '站内转载'):
                validate({**self.payload, 'repost_origin': origin})
        self.assertEqual(validate({**self.payload, 'repost_origin': 'external'})['repost_origin'], 'external')

    def test_private_scheduled_is_rejected(self):
        self.payload.update(publish_mode='scheduled',schedule_time='2026-11-19 10:25')
        with self.assertRaisesRegex(ValueError,'恢复为公开'):validate(self.payload)

    def test_channels_does_not_fabricate_private_mode(self):
        self.payload.update(platform='channels',declaration='无需标注')
        with self.assertRaisesRegex(ValueError,'没有私密'):validate(self.payload)

    def test_long_title_is_not_truncated(self):
        self.payload['title']='中'*21
        with self.assertRaisesRegex(ValueError,'不会自动截断'):validate(self.payload)

    def test_unknown_settings_are_not_ignored(self):
        self.payload['mystery_setting']=True
        with self.assertRaisesRegex(ValueError,'尚未支持'):validate(self.payload)

    def test_false_download_permission_stays_false(self):
        self.assertIs(validate(self.payload)['allow_download'],False)

    def test_schedule_checks_beijing_minute(self):
        now=datetime(2026,11,18,8,0,tzinfo=TZ)
        self.payload.update(visibility='public',publish_mode='scheduled',schedule_time='2026-11-18T10:25')
        self.assertEqual(validate(self.payload,now)['schedule_time'],'2026-11-18 10:25')
        with self.assertRaises(ValueError):validate(self.payload,now+timedelta(minutes=26))

    def test_media_replacement_blocks_verification(self):
        checks=compare(self.payload,self.snapshot,{'images':['blob:other'],'video':''})
        self.assertFalse(next(x['ok'] for x in checks if x['field']=='素材未被替换'))

    def publisher(self):
        database=Path(self.tmp.name)/'test.sqlite3'
        class App:
            BROWSER_LOCK=threading.RLock()
            @contextlib.contextmanager
            def db_connection(self):
                db=sqlite3.connect(database)
                try:yield db;db.commit()
                finally:db.close()
        pub=Publisher(App());pub.init()
        binding=dict(platform='douyin',client_id='client',tab_id=1,account='本人',account_id='test-account-1')
        with pub.app.db_connection() as db:
            db.execute('INSERT INTO platform_bindings VALUES (?,?)',('douyin',json.dumps(binding)))
        pub.uploads[('douyin',1)]=dict(files=[str(self.file)],stats=file_stats([str(self.file)]),media={'images':['blob:one'],'video':''})
        return pub

    def test_verified_receipt_is_invalidated_when_next_check_fails(self):
        pub=self.publisher()
        with patch('platform_publishing.Page') as P:
            P.return_value.tab_id=1;P.return_value.snapshot.return_value=self.snapshot
            first=pub.verify(self.payload)
            self.snapshot['fields']['title']='已改动'
            second=pub.verify(self.payload)
        self.assertTrue(first['verified']);self.assertFalse(second['verified'])
        with pub.app.db_connection() as db:
            status=db.execute('SELECT status FROM platform_receipts WHERE id=?',(first['receipt'],)).fetchone()[0]
        self.assertEqual(status,'invalidated')

    def test_changed_page_cannot_be_submitted(self):
        pub=self.publisher()
        with patch('platform_publishing.Page') as P:
            P.return_value.tab_id=1;P.return_value.snapshot.return_value=self.snapshot
            first=pub.verify(self.payload)
            self.snapshot['radios'][0]='公开'
            with self.assertRaisesRegex(ValueError,'发生变化'):pub.submit({'receipt':first['receipt']})
            P.return_value.click.assert_not_called()

    def test_uncertain_submit_is_never_retried(self):
        pub=self.publisher()
        with patch('platform_publishing.Page') as P:
            P.return_value.tab_id=1;P.return_value.snapshot.return_value=self.snapshot
            first=pub.verify(self.payload)
            P.return_value.click.side_effect=RuntimeError('connection lost after click')
            result=pub.submit({'receipt':first['receipt']})
            self.assertEqual(result['status'],'unknown')
            with self.assertRaisesRegex(ValueError,'用过或失效'):pub.submit({'receipt':first['receipt']})
            self.assertEqual(P.return_value.click.call_count,1)

    def test_changed_local_file_invalidates_upload(self):
        pub=self.publisher();self.file.write_bytes(b'changed media')
        with patch('platform_publishing.Page') as P:
            P.return_value.tab_id=1;P.return_value.snapshot.return_value=self.snapshot
            with self.assertRaisesRegex(ValueError,'上传记录失效'):pub.verify(self.payload)

    def test_blank_settings_do_not_keep_native_defaults(self):
        self.payload.update(music='',location='',collection='')
        checked=validate(self.payload)
        self.assertEqual(checked['music'],'不添加音乐')
        self.snapshot['fields']['music']='平台自动添加的歌曲'
        self.assertFalse(all(x['ok'] for x in compare(checked,self.snapshot)))

    def test_new_receipt_cannot_repeat_uncertain_submission(self):
        pub=self.publisher()
        with patch('platform_publishing.Page') as P:
            P.return_value.tab_id=1;P.return_value.snapshot.return_value=self.snapshot
            P.return_value.click.side_effect=RuntimeError('lost after click')
            first=pub.verify(self.payload);pub.submit({'receipt':first['receipt']})
            again=pub.verify(self.payload)
            with self.assertRaisesRegex(ValueError,'已经尝试提交'):pub.submit({'receipt':again['receipt']})
            self.assertEqual(P.return_value.click.call_count,1)

    def test_changing_tab_does_not_reset_duplicate_guard(self):
        pub=self.publisher();d=validate(self.payload);binding=pub.binding('douyin')
        first=pub.submission_key(d,binding)
        self.assertEqual(first,pub.submission_key(d,{**binding,'tab_id':99}))

    def test_channel_shooting_details_are_required(self):
        self.payload.update(platform='channels',declaration='内容为自行拍摄',visibility='public')
        with self.assertRaisesRegex(ValueError,'拍摄日期'):validate(self.payload)

    def test_placeholder_upload_waits_and_can_resume_without_uploading_again(self):
        pub=self.publisher();pub.uploads[('douyin',1)]['pending']=True
        pending={**self.snapshot,'media_ready':False,'media':['none']}
        with patch('platform_publishing.Page') as P,patch('platform_publishing.time.sleep'):
            page=P.return_value;page.tab_id=1
            page.snapshot.side_effect=[pending]*4+[self.snapshot]*4
            result=pub.upload({'platform':'douyin','media_type':'image','files':[str(self.file)]})
            self.assertTrue(result['snapshot']['media_ready'])
            self.assertFalse(any(c.args and c.args[0]=='files' for c in page.call.call_args_list))
        # A restart keeps the verified upload provenance.
        restarted=Publisher(pub.app);restarted.init()
        self.assertEqual(restarted.uploads[('douyin',1)]['media']['images'],['blob:one'])

    def test_loading_image_never_verifies(self):
        self.snapshot['media_ready']=False
        self.assertFalse(all(x['ok'] for x in compare(self.payload,self.snapshot)))

    def test_native_security_challenge_blocks_submit(self):
        pub=self.publisher()
        with patch('platform_publishing.Page') as P:
            P.return_value.tab_id=1;P.return_value.snapshot.return_value=self.snapshot
            r=pub.verify(self.payload);self.snapshot['risky']=True
            with self.assertRaisesRegex(ValueError,'安全验证'):pub.submit(r)
            P.return_value.click.assert_not_called()

    def test_changed_account_blocks_submit(self):
        pub=self.publisher()
        with patch('platform_publishing.Page') as P:
            P.return_value.tab_id=1;P.return_value.snapshot.return_value=self.snapshot
            r=pub.verify(self.payload);self.snapshot['account']='另一个账号'
            with self.assertRaisesRegex(ValueError,'账号已经变化'):pub.submit(r)
            P.return_value.click.assert_not_called()

    def test_native_combined_douyin_title_reconciles_verified_work_id(self):
        pub=self.publisher()
        row=dict(title='原始标题。第一行。第二行。 #摄影',text='原始标题。第一行。第二行。 #摄影\n已发布',id='actual-id',created_at=1001,status_text='已发布')
        with patch('platform_publishing.Page') as P,patch('platform_publishing.rpc') as rpc:
            P.return_value.snapshot.return_value=self.snapshot
            P.return_value.dom.return_value=[row]
            result=pub.read_result('douyin',self.payload,1000)
        self.assertEqual(result['status'],'published')
        self.assertEqual(result['work_id'],'actual-id');rpc.assert_not_called()

    def test_ambiguous_combined_titles_are_not_confirmed(self):
        pub=self.publisher()
        row=dict(title='原始标题。第一行。第二行。 #摄影',text='原始标题。第一行。第二行。 #摄影\n已发布',id='one',created_at=1001,status_text='已发布')
        with patch('platform_publishing.Page') as P:
            P.return_value.snapshot.return_value={**self.snapshot,'url':'https://creator.douyin.com/creator-micro/content/manage'}
            P.return_value.dom.return_value=[row,{**row,'id':'two'}]
            result=pub.read_result('douyin',self.payload,1000)
        self.assertEqual(result['status'],'unknown');self.assertEqual(result['candidates'],2)

    def test_old_same_title_does_not_confirm_new_attempt(self):
        pub=self.publisher()
        with patch('platform_publishing.Page') as P,patch('platform_publishing.time.sleep'):
            P.return_value.snapshot.return_value={**self.snapshot,'url':'https://creator.douyin.com/creator-micro/content/manage'}
            P.return_value.dom.return_value=[dict(title='原始标题。正文',text='已发布',id='old',created_at=50,status_text='已发布')]
            self.assertEqual(pub.read_result('douyin',self.payload,1000)['status'],'unknown')

    def test_caption_does_not_override_native_status(self):
        pub=self.publisher()
        with patch('platform_publishing.Page') as P:
            P.return_value.snapshot.return_value=self.snapshot
            P.return_value.dom.return_value=[dict(title='原始标题',text='定时验证已发布',description=self.payload['content'],id='work',created_at=1001,status_text='审核中')]
            self.assertEqual(pub.read_result('douyin',self.payload,1000)['status'],'submitted')

    def test_channels_authenticated_home_reuses_matching_native_card(self):
        pub=self.publisher()
        b={**pub.binding('douyin'),'platform':'channels'}
        with pub.app.db_connection() as db:db.execute('INSERT INTO platform_bindings VALUES (?,?)',('channels',json.dumps(b)))
        with patch('platform_publishing.Page') as P,patch('platform_publishing.rpc') as rpc:
            P.return_value.snapshot.return_value={**self.snapshot,'url':'https://channels.weixin.qq.com/platform'}
            P.return_value.dom.return_value=[dict(title=self.payload['content'],text=self.payload['content']+' 定时视频验证 #摄影',id='native-work',created_at=1001,native_status=1)]
            result=pub.read_result('channels',{**self.payload,'media_type':'video'},1000)
        self.assertEqual(result['status'],'published');rpc.assert_not_called()

    def test_open_channels_reuses_empty_authenticated_form(self):
        pub=self.publisher()
        b={**pub.binding('douyin'),'platform':'channels'}
        with pub.app.db_connection() as db:db.execute('INSERT INTO platform_bindings VALUES (?,?)',('channels',json.dumps(b)))
        with patch('platform_publishing.tabs',return_value=[dict(platform='channels',client_id='client',id=1)]),patch.object(pub,'page') as page,patch.object(pub,'bind',return_value={'binding':b}) as bind,patch('platform_publishing.rpc') as rpc:
            page.return_value.snapshot.return_value={'image_count':0,'video':None,'fields':{'content':''}}
            pub.open({'platform':'channels','media_type':'image','client_id':'client'})
            page.return_value.dom.assert_called_once_with('navigate_empty_channels',path='/platform/post/finderNewLifeCreate')
            bind.assert_called_once_with({**b,'expected_account_id':b['account_id']});rpc.assert_not_called()

    def test_open_channels_preserves_populated_draft(self):
        pub=self.publisher()
        b={**pub.binding('douyin'),'platform':'channels'}
        with pub.app.db_connection() as db:db.execute('INSERT INTO platform_bindings VALUES (?,?)',('channels',json.dumps(b)))
        with patch('platform_publishing.tabs',return_value=[dict(platform='channels',client_id='client',id=1)]),patch.object(pub,'page') as page,patch.object(pub,'bind'),patch('platform_publishing.rpc',return_value={'id':2}) as rpc,patch('platform_publishing.time.sleep'):
            page.return_value.snapshot.return_value={'image_count':1,'video':None,'fields':{'content':'已填好的草稿'}}
            pub.open({'platform':'channels','media_type':'image','client_id':'client'})
            page.return_value.dom.assert_not_called();rpc.assert_called_once()

    def test_stale_douyin_manager_refreshes_once_without_publish(self):
        pub=self.publisher()
        row=dict(title='原始标题',description=self.payload['content'],text='已发布',id='new',created_at=1001,status_text='已发布')
        with patch('platform_publishing.Page') as P,patch('platform_publishing.time.sleep'):
            page=P.return_value
            page.snapshot.return_value={**self.snapshot,'url':'https://creator.douyin.com/creator-micro/content/manage'}
            page.dom.side_effect=[[],[row]]
            result=pub.read_result('douyin',self.payload,1000)
            self.assertEqual(result['work_id'],'new');page.call.assert_called_once();page.click.assert_not_called()

    def test_unrelated_or_unidentifiable_work_remains_unknown(self):
        pub=self.publisher()
        row=dict(title='原始标题',text='已发布',description=self.payload['content'],id='new',created_at=1001,status_text='已发布')
        for change in ({'id':''},{'created_at':0},{'created_at':100000},{'description':'其他正文'}):
            with self.subTest(change=change),patch('platform_publishing.Page') as P,patch('platform_publishing.time.sleep'):
                page=P.return_value
                page.snapshot.return_value={**self.snapshot,'url':'https://creator.douyin.com/creator-micro/content/manage'}
                page.dom.return_value=[{**row,**change}]
                self.assertEqual(pub.read_result('douyin',self.payload,1000)['status'],'unknown')


class NativeTopicCommitTests(unittest.TestCase):
    def page(self):
        from platform_bridge import Page
        return Page(dict(platform='douyin',client_id='client',tab_id=1))

    def test_topic_uses_exact_native_option_and_checks_committed_node(self):
        page=self.page()
        def dom(action,**kwargs):
            if action=='topic_exists':return True
            if action=='snapshot':return {'fields':{'tags':['#摄影']}}
        with patch.object(page,'background_input'),patch.object(page,'call'),patch.object(page,'click') as click,patch.object(page,'snapshot',return_value={'fields':{'content':''}}),patch.object(page,'dom',side_effect=dom) as actions,patch('platform_bridge.time.sleep'):
            page.editor('准确正文',['摄影'])
            click.assert_called_once_with(field='content')
            self.assertTrue(any(c.args==('activate',) and c.kwargs.get('text')=='摄影' for c in actions.call_args_list))

    def test_missing_topic_node_stops_after_one_rewrite(self):
        page=self.page()
        with patch.object(page,'_editor_once',side_effect=ValueError('官方话题尚未成为有效节点')) as fill:
            with self.assertRaisesRegex(ValueError,'有效节点'):page.editor('正文',['摄影'])
            self.assertEqual(fill.call_count,2)

    def test_background_input_stays_attached_to_the_bound_tab(self):
        from platform_bridge import background_session
        page=self.page()
        with patch('platform_bridge.rpc') as rpc, self.assertRaises(ValueError):
            with background_session():
                page.background_input()
                self.page().background_input()
                page.call('insert_text',text='定向输入')
                raise ValueError('test')
        calls=rpc.call_args_list
        self.assertEqual([c.args[2] for c in calls],['background_begin','insert_text','background_end'])
        self.assertEqual(len({c.kwargs['session'] for c in calls}),1)
        self.assertTrue(all(c.kwargs['tab_id']==1 for c in calls))

    def test_old_extension_requires_reload_before_background_open(self):
        from platform_bridge import rpc
        with patch('platform_bridge.browser_profiles.control',return_value={'capabilities':{'client':['platforms_v1']}}),patch('platform_bridge.connect') as connect:
            with self.assertRaisesRegex(ValueError,'重新加载'):
                rpc('client','douyin','open_background',url='https://creator.douyin.com')
            connect.assert_not_called()

    def test_new_draft_cannot_rebind_an_automatic_task_to_another_account(self):
        from unittest.mock import Mock
        pub=Publisher(Mock())
        with patch('platform_publishing.Page') as P,patch('platform_publishing.tabs',return_value=[{'platform':'douyin','client_id':'client','id':2}]):
            P.return_value.snapshot.return_value={'account':'另一个账号','account_id':'another'}
            with self.assertRaisesRegex(ValueError,'新发布页的账号'):
                pub.bind({'platform':'douyin','client_id':'client','tab_id':2,'expected_account_id':'original'})
            pub.app.db_connection.assert_not_called()


if __name__=='__main__':unittest.main()
