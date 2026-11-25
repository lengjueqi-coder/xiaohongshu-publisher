"""Regression coverage for irreversible publish boundaries and durable recovery."""
import contextlib,json,sqlite3,tempfile,threading,unittest,time
from pathlib import Path
from unittest.mock import Mock,patch
import platform_publishing_tests as publishing_fixtures
from distribution_service import DistributionService
from distribution_plans import DailyPlans,init_tables
import vision_service

class PublisherRegressions(unittest.TestCase):
    setUp=publishing_fixtures.PlatformPublishingTests.setUp
    publisher=publishing_fixtures.PlatformPublishingTests.publisher
    def test_file_replaced_after_verify_never_clicks(self):
        pub=self.publisher()
        with patch('platform_publishing.Page') as P:
            p=P.return_value;p.tab_id=1;p.snapshot.return_value=self.snapshot
            receipt=pub.verify(self.payload);self.file.write_bytes(b'replaced after review')
            with self.assertRaisesRegex(ValueError,'素材在核对后'):pub.submit(receipt)
            p.click.assert_not_called()

    def test_empty_identity_fails_closed(self):
        pub=self.publisher()
        with pub.app.db_connection() as db:db.execute('UPDATE platform_bindings SET binding=?',(json.dumps({'platform':'douyin','client_id':'client','tab_id':1,'account':''}),))
        with patch('platform_publishing.Page') as P:
            P.return_value.snapshot.return_value=self.snapshot
            with self.assertRaisesRegex(ValueError,'身份未核实'):pub.page('douyin')

    def test_ignored_field_cannot_bypass_dedupe(self):
        pub=self.publisher();binding=pub.binding('douyin')
        self.assertEqual(pub.submission_key(self.payload,binding),pub.submission_key({**self.payload,'short_title':'unused'},binding))

    def test_same_bytes_in_other_task_folder_have_same_submission_key(self):
        pub=self.publisher();binding=pub.binding('douyin')
        other=self.file.parent/'renamed.jpg';other.write_bytes(self.file.read_bytes())
        self.assertEqual(pub.submission_key(self.payload,binding),pub.submission_key({**self.payload,'files':[str(other)]},binding))

class DurableBatches(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.db=self.root/'test.db';self.image=self.root/'one.jpg';self.image.write_bytes(b'original')
        outer=self
        class App:
            APP_ROOT=outer.root;BROWSER_LOCK=threading.RLock();PublishError=RuntimeError
            @contextlib.contextmanager
            def db_connection(self):
                db=sqlite3.connect(outer.db)
                try:yield db;db.commit()
                finally:db.close()
            def active_profile_dir(self):return 'profile'
        self.app=App();self.pub=Mock();self.pub.binding.return_value={'platform':'douyin','account_id':'123','account':'test','client_id':'client','tab_id':1}
        self.service=DistributionService(self.app,self.pub)
        self.payload={'platform':'douyin','media_type':'image','title':'测试','content':'内容','files':[str(self.image)],'tags':[],'declaration':'无需添加自主声明','visibility':'public'}
    def create(self,id='id',paused=True):return self.service.create({'request_id':id,'targets':{'douyin':dict(self.payload)},'paused':paused})['batches'][0]
    def test_confirmation_freezes_material_and_replayed_request_creates_no_duplicate(self):
        first=self.create();self.image.write_bytes(b'changed by editor')
        second=self.create()
        self.assertEqual(first['id'],second['id']);frozen=Path(first['steps'][0]['payload']['files'][0]);self.assertEqual(frozen.read_bytes(),b'original')

    def test_sync_preserves_reviewed_files_through_directory_alias(self):
        import server
        from PIL import Image
        alias=self.root/'alias'
        stage=self.root/'staging'
        stage.mkdir()
        alias.symlink_to(stage,target_is_directory=True)
        self.app.APP_ROOT=alias
        self.app.require_active_profile=lambda draft:'profile'
        self.app.account_identity=lambda profile:{'user_id':'xhs-one'}
        self.app.validate_note_image_count=server.validate_note_image_count
        self.app.freeze_payload=server.freeze_payload
        Image.new('RGB',(8,8),'white').save(self.image)
        draft={'images':[str(self.image)],'title':'标题','content':'正文','tags':[]}
        with patch.object(server,'account_setting',side_effect=lambda key,default=None,profile_dir=None:default):
            result=self.service.create({'request_id':'aliased','sync_from':'xhs','prepare_only':True,
                'targets':{'xhs':{'media_type':'image','payload':draft},'douyin':self.payload}})
        xhs,dy=result['batches'][0]['steps']
        frozen=xhs['payload']['payload']
        server.verify_frozen_images(frozen)
        self.assertEqual(frozen['images'],dy['payload']['files'])
        self.assertEqual(list((alias/'data'/'distribution'/xhs['batch_id']).glob('*.jpg')),[])
    def test_restart_does_not_repeat_inflight_click(self):
        item=self.create(paused=False);self.service.set_step(item['id'],'douyin','submitting')
        restarted=DistributionService(self.app,self.pub)
        self.assertEqual(restarted.snapshot(batch_id=item['id'])['batches'][0]['steps'][0]['state'],'unknown')
        with self.assertRaisesRegex(ValueError,'先核对'):restarted.resume(item['id'])
        self.pub.submit.assert_not_called()
    def test_definite_preclick_rejection_is_retryable(self):
        item=self.create(paused=False);self.pub.prepare.return_value={'verified':True,'receipt':'token'};self.pub.submit.side_effect=ValueError('expired before click')
        self.service.execute(item['id']);result=self.service.snapshot(batch_id=item['id'])['batches'][0]
        self.assertEqual(result['steps'][0]['state'],'failed');self.service.resume(item['id'])
        self.assertEqual(self.service.snapshot(batch_id=item['id'])['batches'][0]['state'],'queued')
    def test_postclick_unknown_prevents_second_execution(self):
        item=self.create(paused=False);self.pub.prepare.return_value={'verified':True,'receipt':'token'};self.pub.submit.return_value={'status':'unknown','message':'connection after click'}
        self.service.execute(item['id']);self.service.execute(item['id']);self.assertEqual(self.pub.submit.call_count,1)
    def test_changed_account_rejected_before_prepare(self):
        item=self.create(paused=False);self.pub.binding.return_value={'account_id':'different'};self.service.execute(item['id']);self.pub.prepare.assert_not_called();self.pub.submit.assert_not_called()

    def test_account_changed_since_confirmation_never_creates_batch(self):
        with self.assertRaisesRegex(ValueError,'账号已切换'):
            self.service.create({'request_id':'review','targets':{'douyin':self.payload},'expected_accounts':{'douyin':'old'}})
        self.assertEqual(self.service.snapshot()['batches'],[])

    def test_retry_of_failed_prefill_waits_for_publication_confirmation(self):
        batch=self.service.create({'request_id':'review','targets':{'douyin':self.payload},'prepare_only':True})['batches'][0]['id']
        self.pub.prepare.side_effect=[ValueError('page loading'),{'verified':True,'receipt':'review'},{'verified':True,'receipt':'submit'}]
        self.service.execute(batch)
        self.service.resume(batch)
        self.service.execute(batch)
        self.assertEqual(self.service.snapshot(batch_id=batch)['batches'][0]['state'],'paused')
        self.pub.submit.assert_not_called()
        self.service.resume(batch)
        self.pub.submit.return_value={'status':'submitted','message':'received'}
        self.service.execute(batch)
        self.pub.submit.assert_called_once_with({'receipt':'submit'})

    def test_continue_remaining_skips_unknown_platform(self):
        item=self.create(paused=False);self.service.set_step(item['id'],'douyin','unknown');self.service.set_batch(item['id'],'attention')
        self.service.resume(item['id'],remaining_only=True);self.service.execute(item['id'])
        self.pub.prepare.assert_not_called();self.pub.submit.assert_not_called()
        self.assertEqual(self.service.snapshot(batch_id=item['id'])['batches'][0]['state'],'attention')

    def test_official_reconciliation_retains_work_id_and_completes_batch(self):
        item=self.create();self.service.set_step(item['id'],'douyin','submitted',receipt='attempt')
        with self.app.db_connection() as db:
            db.execute('CREATE TABLE platform_attempts (receipt TEXT, created_at REAL)')
        with self.assertRaisesRegex(ValueError,'实际提交时间'):
            self.service.reconcile(item['id'],'douyin')
        with self.app.db_connection() as db:
            db.execute('INSERT INTO platform_attempts VALUES (?,?)',('attempt',123456))
        self.pub.read_result.return_value={'status':'published','message':'official match','work_id':'actual-work-id'}
        r=self.service.reconcile(item['id'],'douyin')['batches'][0]
        self.assertEqual(r['state'],'published');self.assertEqual(r['steps'][0]['result']['work_id'],'actual-work-id')
        self.assertEqual(self.pub.read_result.call_args.args[2],123456)
        self.pub.submit.assert_not_called()

class VisionProtocols(unittest.TestCase):
    def configuration(self,protocol):return {'provider':'custom','protocol':protocol,'base_url':'https://example.com/v1','model':'vision','api_key':'private-key','max_images':2}
    def test_chat_image_and_secret_only_in_authorization(self):
        response=Mock(ok=True);response.json.return_value={'choices':[{'message':{'content':'{"color":"red"}'}}]}
        with patch('vision_service.requests.post',return_value=response) as post:
            self.assertEqual(vision_service.invoke(self.configuration('openai'),'prompt',['encoded'])['color'],'red')
            kwargs=post.call_args.kwargs;self.assertNotIn('private-key',json.dumps(kwargs['json']));self.assertEqual(kwargs['headers']['Authorization'],'Bearer private-key')
            self.assertEqual(kwargs['json']['messages'][0]['content'][1]['type'],'image_url')
    def test_gemini_native_uses_inline_frames_and_header_key(self):
        response=Mock(ok=True);response.json.return_value={'candidates':[{'content':{'parts':[{'text':'{"color":"red"}'}]}}]}
        with patch('vision_service.requests.post',return_value=response) as post:
            vision_service.invoke(self.configuration('gemini'),'prompt',['encoded']);self.assertIn('inline_data',post.call_args.kwargs['json']['contents'][0]['parts'][1]);self.assertNotIn('private-key',post.call_args.args[0])
    def test_responses_protocol_reads_output_text(self):
        response=Mock(ok=True);response.json.return_value={'output':[{'content':[{'type':'output_text','text':'{"color":"red"}'}]}]}
        with patch('vision_service.requests.post',return_value=response) as post:
            self.assertEqual(vision_service.invoke(self.configuration('responses'),'prompt',['encoded']),{'color':'red'})
            self.assertTrue(post.call_args.args[0].endswith('/responses'))
    def test_missing_key_does_not_call_provider(self):
        c=self.configuration('openai');c['api_key']=''
        with patch('vision_service.requests.post') as post:
            with self.assertRaisesRegex(ValueError,'密钥'):vision_service.invoke(c,'test')
            post.assert_not_called()
    def test_switching_api_host_never_reuses_old_service_secret(self):
        app=Mock();app.db_get_setting.return_value=self.configuration('openai')
        changed=vision_service.config(app,{'base_url':'https://other-provider.com/v1'})
        self.assertEqual(changed['api_key'],'')
        same=vision_service.config(app,{'model':'another-vision-model'})
        self.assertEqual(same['api_key'],'private-key')
    def test_redirect_or_quota_failure_never_includes_secret(self):
        response=Mock(ok=False,status_code=429)
        with patch('vision_service.requests.post',return_value=response):
            with self.assertRaisesRegex(ValueError,'HTTP 429') as error:vision_service.invoke(self.configuration('openai'),'test')
        self.assertNotIn('private-key',str(error.exception))

if __name__=='__main__':unittest.main()

class DailyPlanChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.db=self.root/'data.db'
        outer=self
        class App:
            APP_ROOT=outer.root;BROWSER_LOCK=threading.RLock();PublishError=RuntimeError
            @contextlib.contextmanager
            def db_connection(self):
                db=sqlite3.connect(outer.db)
                try:yield db;db.commit()
                finally:db.close()
            def active_profile_dir(self):return 'profile'
            def account_setting(self,key,default=None,profile=None):return default
            def profile_prompt_block(self,profile):return 'confirmed persona'
        self.app=App();self.publisher=Mock();self.publisher.binding.return_value={'account_id':'user','account':'test'}
        self.service=DistributionService(self.app,self.publisher);self.plans=DailyPlans(self.app,self.service)
        with self.app.db_connection() as db:db.execute('CREATE TABLE automation_plans(profile_dir TEXT,enabled INTEGER)')
        folder=self.root/'materials'/'one';folder.mkdir(parents=True)
        f=folder/'image.jpg';f.write_bytes(b'image');import os;os.utime(f,(time.time()-120,time.time()-120))
        (folder/'发布文案.json').write_text(json.dumps({'title':'测试文案','content':'这是一条人工确认的文案'},ensure_ascii=False))
        self.config={'root_path':str(folder.parent),'targets':['douyin'],'copy_mode':'sidecar','daily_count':1,'times':['21:00']}
    def test_preview_never_runs_until_confirmed_and_material_not_selected_twice(self):
        self.plans.save('profile',self.config);result=self.plans.build('profile')['batches'][0]
        self.assertEqual(result['state'],'paused');self.publisher.prepare.assert_not_called()
        self.assertEqual(self.plans.scan('profile'),[])
    def test_automatic_daily_count_is_bounded(self):
        with self.assertRaisesRegex(ValueError,'1～2'):self.plans.save('profile',{**self.config,'daily_count':3})
    def test_vision_is_optional_without_api_key(self):
        self.plans.save('profile',{**self.config,'use_vision':False})
        self.app.db_get_setting=lambda key,default=None:default
        with self.assertRaisesRegex(ValueError,'密钥'):self.plans.save('profile',{**self.config,'use_vision':True})
    def test_incomplete_file_is_not_picked(self):
        import os
        f=self.root/'materials'/'one'/'image.jpg';os.utime(f,None)
        self.plans.save('profile',self.config);self.assertEqual(self.plans.scan('profile'),[])

    def test_excess_photos_wait_for_next_group_and_rename_does_not_repeat(self):
        import os
        folder=self.root/'materials'/'one'
        for i in range(3):
            p=folder/f'{i}.jpg';p.write_bytes(f'image {i}'.encode());os.utime(p,(time.time()-120,time.time()-120))
        self.plans.save('profile',{**self.config,'max_images':2})
        self.assertEqual([len(g['files']) for g in self.plans.scan('profile')],[2,2])
        result=self.plans.build('profile')['batches'][0]
        for name in ('0.jpg','1.jpg'):(folder/name).rename(folder/('renamed'+name))
        groups=self.plans.scan('profile');self.assertEqual(sum(len(g['files']) for g in groups),2)
        self.service.cancel(result['id'])
        self.assertEqual(sum(len(g['files']) for g in self.plans.scan('profile')),4)
        self.assertEqual(self.plans.build('profile')['batches'][0]['state'],'paused')

    def test_daily_scheduler_reserves_each_slot_once_and_uses_native_schedule(self):
        from datetime import datetime,timedelta
        from platform_publishing import TZ
        self.plans.save('profile',self.config);self.plans.enabled('profile',True)
        at=(datetime.now(TZ)+timedelta(days=1)).replace(hour=17,minute=0,second=0,microsecond=0)
        self.plans.tick(at);self.plans.tick(at)
        r=self.service.snapshot('profile')['batches'];self.assertEqual(len(r),1)
        self.assertEqual(r[0]['state'],'paused');d=r[0]['steps'][0]['payload']
        self.assertEqual(d['publish_mode'],'scheduled');self.assertTrue(d['schedule_time'].endswith('21:00'))
        self.pub_submit_never()

    def test_daily_plan_pauses_after_unknown_submit(self):
        self.plans.save('profile',self.config);self.plans.enabled('profile',True)
        batch=self.plans.build('profile',automatic=True)['batches'][0]
        self.publisher.prepare.return_value={'verified':True,'receipt':'token'}
        self.publisher.submit.return_value={'status':'unknown','message':'SMS needed'}
        self.service.execute(batch['id'])
        self.assertFalse(self.plans.get('profile')['config']['enabled'])
        self.assertEqual(self.publisher.submit.call_count,1)

    def test_material_change_during_ai_generation_creates_no_task(self):
        self.plans.save('profile',{**self.config,'copy_mode':'ai'})
        source=self.root/'materials'/'one'/'image.jpg'
        def generate(*args):
            source.write_bytes(b'new image saved while model was running')
            return {'title':'新文案','content':'依据拍摄事实生成的正文'}
        with patch('distribution_plans.vision_service.generate',side_effect=generate):
            with self.assertRaisesRegex(ValueError,'生成文案期间素材发生变化'):self.plans.build('profile',automatic=True)
        self.assertEqual(self.service.snapshot('profile')['batches'],[])
        self.publisher.prepare.assert_not_called()

    def test_material_reservation_conflict_rolls_back_queued_batch(self):
        self.plans.save('profile',self.config);group=self.plans.scan('profile')[0]
        with self.app.db_connection() as db:db.execute('INSERT INTO distribution_assets VALUES (?,?,?)',('profile',group['hashes'][0],'other-task'))
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.create({'request_id':'conflict','targets':{'douyin':{'platform':'douyin','media_type':'image','title':'标题','content':'正文','files':group['files'],'tags':[],'declaration':'无需添加自主声明'}},'source_key':group['fingerprint']},reservation={**group,'slot':None})
        self.assertEqual(self.service.snapshot('profile')['batches'],[])

    def pub_submit_never(self):self.publisher.submit.assert_not_called()


class XHSReadbackChecks(unittest.TestCase):
    def test_title_cannot_prove_review_state_or_privacy(self):
        from publish_checks import match_receipt
        for title in ['已发布','审核中','仅自己可见','定时发布','草稿']:
            card={'title':title,'text':title+'\n2026-11-18 23:50','time':'2026-11-18 23:50'}
            result=match_receipt([card],{'title':title,'receipt_reference_time':'2026-11-18T23:50'})
            self.assertEqual(result['status'],'unconfirmed')

    def test_published_filter_and_stable_note_id_confirm_private_publication(self):
        from publish_checks import match_receipt
        row={'title':'视频验证','text':'仅自己可见\n视频验证\n2026-11-18 23:50\n0','private':True,'time':'2026-11-18 23:50','note_id':'a'*24,'published_context':True}
        payload={'title':'视频验证','visibility':'仅自己可见','receipt_reference_time':'2026-11-18T23:50'}
        self.assertEqual(match_receipt([row],payload)['status'],'published')
        row['published_context']=False
        self.assertEqual(match_receipt([row],payload)['status'],'unconfirmed')

    def test_video_replacement_and_schedule_separator_are_checked(self):
        from publish_checks import validate_state
        payload={'title':'标题','content':'正文','media_type':'video','images':[],'native_video_src':'blob:one','visibility':'仅自己可见','original':False,'schedule_time':'2026-11-19 09:30'}
        state={'title':'标题','content':'正文','video':{'src':'blob:one','duration':8},'visibility':'仅自己可见','original':False,'schedule':'2026-11-19T09:30','schedule_on':True}
        validate_state(state,payload)
        with self.assertRaisesRegex(ValueError,'视频在核对后发生变化'):validate_state({**state,'video':{'src':'blob:other','duration':8}},payload)
