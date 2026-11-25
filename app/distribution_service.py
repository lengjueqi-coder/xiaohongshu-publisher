"""Durable publication batches. Browser actions have at most one uncertain attempt."""
from __future__ import annotations
import hashlib
import json
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from platform_publishing import validate, file_stats, digest
import platform_xhs

NAMES={'xhs':'小红书','douyin':'抖音','channels':'视频号'}
TERMINAL={'submitted','published','scheduled','cancelled'}


def init_tables(db):
    db.execute('CREATE TABLE IF NOT EXISTS distribution_results (batch_id TEXT NOT NULL,platform TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(batch_id,platform))')
    db.execute('''CREATE TABLE IF NOT EXISTS distribution_batches (
        id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, profile TEXT NOT NULL,
        state TEXT NOT NULL, source_key TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
        updated_at REAL NOT NULL, message TEXT NOT NULL DEFAULT '')''')
    db.execute('''CREATE TABLE IF NOT EXISTS distribution_steps (
        batch_id TEXT NOT NULL, platform TEXT NOT NULL, payload TEXT NOT NULL,
        binding TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT NOT NULL DEFAULT '',
        message TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
        PRIMARY KEY(batch_id,platform))''')
    if 'prepare_only' not in {r[1] for r in db.execute('PRAGMA table_info(distribution_batches)')}:
        db.execute('ALTER TABLE distribution_batches ADD COLUMN prepare_only INTEGER NOT NULL DEFAULT 0')


class DistributionService:
    def __init__(self,app,publisher):
        self.app=app;self.publisher=publisher;self.stop=threading.Event();self.wake=threading.Event()
        self.lock=threading.RLock();self.thread=None;self.active=''
        with app.db_connection() as db:
            init_tables(db)
            db.execute("UPDATE distribution_steps SET state='unknown',message='软件在提交阶段退出，请先核对官方作品，禁止自动重发' WHERE state='submitting'")
            db.execute("UPDATE distribution_steps SET state='queued',message='已恢复尚未提交的任务' WHERE state='preparing'")
            db.execute("UPDATE distribution_batches SET state='queued' WHERE state='running'")
            db.execute("UPDATE distribution_batches SET state='attention',message='存在待核实的提交' WHERE id IN (SELECT batch_id FROM distribution_steps WHERE state='unknown')")

    def snapshot(self,profile=None,batch_id=None):
        with self.app.db_connection() as db:
            db.row_factory=sqlite3.Row
            if batch_id:rows=db.execute('SELECT * FROM distribution_batches WHERE id=?',(batch_id,)).fetchall()
            else:rows=db.execute('SELECT * FROM distribution_batches WHERE profile=? ORDER BY created_at DESC LIMIT 50',(profile or self.app.active_profile_dir(),)).fetchall()
            result=[]
            for row in rows:
                item=dict(row);steps=db.execute('SELECT * FROM distribution_steps WHERE batch_id=? ORDER BY rowid',(row['id'],)).fetchall()
                item['steps']=[{**dict(step),'payload':json.loads(step['payload']),'binding':json.loads(step['binding'])} for step in steps]
                for step in item['steps']:
                    evidence=db.execute('SELECT result FROM distribution_results WHERE batch_id=? AND platform=?',(row['id'],step['platform'])).fetchone()
                    if evidence:step['result']=json.loads(evidence[0])
                result.append(item)
        return {'batches':result,'busy':bool(self.active)}

    def preflight(self,request):
        profile=str(request.get('profile_dir') or self.app.active_profile_dir())
        targets=request.get('targets')
        if not isinstance(targets,dict) or not targets or set(targets)-set(NAMES):raise ValueError('请选择至少一个有效分发平台')
        prepared={};bindings={}
        for platform,raw in targets.items():
            if platform=='xhs':
                if raw.get('media_type') not in ('image','video'):raise ValueError('小红书素材类型无效')
                d=dict(raw.get('payload') or {});d['profile_dir']=profile
                self.app.require_active_profile(d)
                identity=self.app.account_identity(profile)
                if not identity.get('user_id'):raise ValueError('请在小红书模块先识别当前账号')
                expected=(request.get('expected_accounts') or {}).get('xhs')
                if expected and expected!=identity['user_id']:
                    raise ValueError('小红书账号已切换，请重新确认')
                if raw['media_type']=='image':self.app.validate_note_image_count(d.get('images',[]),require_one=True)
                else:
                    video=Path(d.get('video_path',''))
                    if not video.is_file() or video.suffix.lower() not in ('.mp4','.mov','.m4v','.webm'):raise ValueError('请选择有效的小红书视频')
                if not str(d.get('title','')).strip() or not str(d.get('content','')).strip():raise ValueError('小红书标题、正文不能为空')
                if len(d['title'])>20 or len(d['content'])>1000 or len(d.get('tags',[]))>10:raise ValueError('小红书文案或话题超限')
                bindings[platform]={'profile_dir':profile,'account_id':identity['user_id'],'account':identity.get('nickname') or profile}
                prepared[platform]={'media_type':raw['media_type'],'payload':d}
            else:
                d=validate(raw)
                if d['platform']!=platform:raise ValueError('分发目标与发布参数不一致')
                binding=self.publisher.binding(platform)
                expected=(request.get('expected_accounts') or {}).get(platform)
                if expected and expected!=binding.get('account_id'):
                    raise ValueError(NAMES[platform]+'账号已切换，请重新确认')
                if not binding.get('account_id'):raise ValueError(NAMES[platform]+'账号尚未核实，请重新连接')
                bindings[platform]=binding;prepared[platform]=d
        return profile,prepared,bindings

    def create(self,request,reservation=None):
        request_id=str(request.get('request_id') or '').strip()
        if not request_id or len(request_id)>160:raise ValueError('缺少有效的确认请求编号')
        with self.lock:
            with self.app.db_connection() as db:
                existing=db.execute('SELECT id FROM distribution_batches WHERE request_id=?',(request_id,)).fetchone()
            if existing:return self.snapshot(batch_id=existing[0])
            profile,targets,bindings=self.preflight(request)
            batch=uuid.uuid4().hex;folder=self.app.APP_ROOT/'data'/'distribution'/batch
            folder.mkdir(parents=True)
            frozen={};asset_hashes=[]
            try:
                if request.get('sync_from') == 'xhs' and targets['xhs']['media_type'] == 'image':
                    source = targets['xhs']['payload']
                    source = self.app.freeze_payload({**source, 'material_mode':'mixed'}, folder/'reviewed')
                    targets['xhs']['payload'] = source
                    for platform in targets.keys() - {'xhs'}:
                        targets[platform] = validate({**targets[platform], 'files':source['images'], 'tags':source['tags']})
                    frozen.update((str(Path(path).resolve()),path) for path in source['images'])
                for platform,raw in targets.items():
                    d=raw['payload'] if platform=='xhs' else raw
                    if platform=='xhs':
                        # Distribution tracks asset use itself. A legacy folder
                        # archive must never move the source of another platform.
                        d['material_mode']='mixed';d['archive_info']={'skip':'多平台分发保留原素材，由素材使用记录防重'}
                    video=platform=='xhs' and raw['media_type']=='video'
                    key='images' if platform=='xhs' else 'files'
                    if video:d['images']=[d['video_path']]
                    files=[]
                    for name in d[key]:
                        path=Path(name).expanduser().resolve()
                        if str(path) not in frozen:
                            before=file_stats([str(path)])
                            asset_hashes.append(before[0][3])
                            target=folder/(f'{len(frozen)+1:02d}'+path.suffix.lower())
                            shutil.copy2(path,target)
                            if before!=file_stats([str(path)]):raise ValueError('复制过程中素材发生变化，请等待文件保存完成')
                            frozen[str(path)]=str(target)
                        files.append(frozen[str(path)])
                    d[key]=files
                    if video:d['video_path']=files[0];d['images']=[]
                if reservation and set(asset_hashes)!=set(reservation['hashes']):raise ValueError('生成文案期间素材发生变化，请等待保存完成后重新制作')
                now=time.time()
                with self.app.db_connection() as db:
                    initial='paused' if request.get('paused') is True else 'queued'
                    db.execute('INSERT INTO distribution_batches VALUES (?,?,?,?,?,?,?,?,?)',(batch,request_id,profile,initial,str(request.get('source_key','')),now,now,'',int(request.get('prepare_only') is True)))
                    for platform,d in targets.items():db.execute('INSERT INTO distribution_steps VALUES (?,?,?,?,?,?,?,?)',(batch,platform,json.dumps(d,ensure_ascii=False),json.dumps(bindings[platform],ensure_ascii=False),'queued','','',now))
                    if reservation:
                        db.execute('INSERT INTO distribution_materials VALUES (?,?,?,?)',(profile,reservation['fingerprint'],reservation['folder'],batch))
                        db.executemany('INSERT INTO distribution_assets VALUES (?,?,?)',[(profile,h,batch) for h in set(reservation['hashes'])])
                        if reservation.get('slot'):db.execute('INSERT INTO distribution_slots VALUES (?,?,?)',(profile,reservation['slot'],batch))
            except Exception:
                shutil.rmtree(folder,ignore_errors=True);raise
            self.wake.set()
            return self.snapshot(batch_id=batch)

    def set_step(self,batch,platform,state,message='',receipt=None):
        with self.app.db_connection() as db:
            if receipt is None:db.execute('UPDATE distribution_steps SET state=?,message=?,updated_at=? WHERE batch_id=? AND platform=?',(state,str(message)[:1800],time.time(),batch,platform))
            else:db.execute('UPDATE distribution_steps SET state=?,message=?,receipt=?,updated_at=? WHERE batch_id=? AND platform=?',(state,str(message)[:1800],receipt,time.time(),batch,platform))
            db.execute('UPDATE distribution_batches SET updated_at=? WHERE id=?',(time.time(),batch))

    def set_batch(self,batch,state,message=''):
        with self.app.db_connection() as db:db.execute('UPDATE distribution_batches SET state=?,message=?,updated_at=? WHERE id=?',(state,str(message)[:1800],time.time(),batch))

    def aggregate(self,batch):
        steps=self.snapshot(batch_id=batch)['batches'][0]['steps']
        states={s['state'] for s in steps}
        if states <= {'published','scheduled'}:
            state='scheduled' if 'scheduled' in states else 'published'
            self.set_batch(batch,state,'各平台作品结果已核实；官方定时作品将在所选时间发表')
        elif states <= {'submitted','published','scheduled'}:
            self.set_batch(batch,'submitted','各平台已接收；仍有最终审核状态待核实')
        else:self.set_batch(batch,'attention','已记录核对结果，可继续尚未提交的平台')

    def attention(self,batch,message):
        self.set_batch(batch,'attention',message)
        item=self.snapshot(batch_id=batch)['batches'][0]
        with self.app.db_connection() as db:
            if item['source_key'] and db.execute("SELECT 1 FROM sqlite_master WHERE name='distribution_plans'").fetchone():
                row=db.execute('SELECT config FROM distribution_plans WHERE profile=?',(item['profile'],)).fetchone()
                if row:
                    config=json.loads(row[0]);config['enabled']=False
                    db.execute('UPDATE distribution_plans SET config=?,error=?,updated_at=? WHERE profile=?',(json.dumps(config,ensure_ascii=False),str(message)[:1000],time.time(),item['profile']))
        if hasattr(self.app,'now_iso'):
            import mac_notifications
            try:
                with self.app.db_connection() as db:mac_notifications.init_tables(db)
                mac_notifications.add_alert(self.app,item['profile'],batch,'distribution_attention','多平台分发需要处理：'+str(message))
            except (OSError,sqlite3.Error):pass

    def check_binding(self,platform,binding):
        if platform=='xhs':
            if self.app.active_profile_dir()!=binding['profile_dir']:raise ValueError('小红书当前账号已切换，请切回原账号后继续')
            identity=self.app.account_identity(binding['profile_dir'])
            if identity.get('user_id')!=binding['account_id']:raise ValueError('小红书绑定账号变化，任务已停止')
        else:
            current=self.publisher.binding(platform)
            if current.get('account_id')!=binding['account_id']:raise ValueError(NAMES[platform]+'账号已变化，任务已停止')

    def execute(self,batch):
        with self.app.db_connection() as db:
            claimed=db.execute("UPDATE distribution_batches SET state='running' WHERE id=? AND state='queued'",(batch,)).rowcount
        if not claimed:return
        self.active=batch
        try:
            item=self.snapshot(batch_id=batch)['batches'][0]
            steps=item['steps']
            # Prepare every unsubmitted platform before any new publish click.
            for step in steps:
                if step['state'] in TERMINAL or step['state']=='unknown':continue
                if step['state']=='submitting':raise ValueError('已有提交进行中，不能重复操作')
                platform=step['platform'];self.set_step(batch,platform,'preparing','上传素材并填写、核对官方页面')
                try:
                    with self.app.BROWSER_LOCK:
                        self.check_binding(platform,step['binding'])
                        if platform=='xhs':result=platform_xhs.prepare(self.app,step['payload'])
                        else:result=self.publisher.prepare(step['payload'])
                    if not result.get('verified'):
                        failures=[c['field'] for c in result.get('checks',[]) if not c['ok']]
                        raise ValueError('核对未通过：'+','.join(failures))
                    step['receipt']=result['receipt'];self.set_step(batch,platform,'verified','官方页面已核对，尚未点击提交',step['receipt'])
                except Exception as exc:
                    self.set_step(batch,platform,'failed',str(exc));raise
            if item['prepare_only']:
                self.set_batch(batch,'paused','全部所选平台已填写并核对，等待确认发布')
                return
            for step in steps:
                if step['state'] in TERMINAL or step['state']=='unknown':continue
                platform=step['platform']
                with self.app.BROWSER_LOCK:
                    try:
                        self.check_binding(platform,step['binding'])
                    except Exception as exc:
                        self.set_step(batch,platform,'failed',str(exc));raise
                    self.set_step(batch,platform,'submitting','提交阶段，请等待；退出后会保留待核对状态')
                    try:
                        result=platform_xhs.submit(self.app,{'receipt':step['receipt']}) if platform=='xhs' else self.publisher.submit({'receipt':step['receipt']})
                    except (ValueError,self.app.PublishError) as exc:
                        # Publishers raise these only before a click; post-click failures
                        # are explicitly returned as unknown and must never be retried.
                        self.set_step(batch,platform,'failed',str(exc));raise
                    except Exception as exc:
                        self.set_step(batch,platform,'unknown','提交异常，需查看官方实际结果：'+str(exc));raise
                    self.set_step(batch,platform,result['status'],result['message'])
                    if result['status']!='submitted':raise ValueError('已尝试提交，结果待核实；其余平台尚未提交')
            if any(s['state']=='unknown' for s in steps):self.attention(batch,'其余平台已处理；仍有平台结果待核实，不会重发')
            else:self.set_batch(batch,'submitted','所选平台已接收提交，最终审核和发布时间以官方作品为准')
        except Exception as exc:self.attention(batch,str(exc))
        finally:self.active=''

    def resume(self,batch,remaining_only=False):
        with self.lock:
            item=self.snapshot(batch_id=batch)['batches']
            if not item:raise ValueError('任务不存在')
            if item[0]['state'] not in ('attention','paused'):raise ValueError('此任务当前不能继续')
            if any(s['state']=='submitting' for s in item[0]['steps']):raise ValueError('仍有提交正在处理，请等待')
            if not remaining_only and any(s['state']=='unknown' for s in item[0]['steps']):raise ValueError('请先核对官方实际结果；或明确只继续尚未提交的平台')
            with self.app.db_connection() as db:
                if item[0]['state']=='paused':
                    db.execute("UPDATE distribution_batches SET prepare_only=0 WHERE id=?",(batch,))
                db.execute("UPDATE distribution_steps SET state='queued',receipt='' WHERE batch_id=? AND state NOT IN ('submitted','published','scheduled','cancelled','unknown')",(batch,))
            self.set_batch(batch,'queued');self.wake.set()
        return self.snapshot(batch_id=batch)

    def cancel(self,batch):
        with self.lock:
            if self.active==batch:raise ValueError('任务正在操作官方页面，请等待当前操作结束后处理')
            item=self.snapshot(batch_id=batch)['batches']
            if not item:raise ValueError('任务不存在')
            if any(s['state'] in ('submitting','unknown','submitted','published','scheduled') for s in item[0]['steps']):
                raise ValueError('内容已有提交尝试，不能当作未发布任务取消；请在官方作品管理处理')
            with self.app.db_connection() as db:
                db.execute("UPDATE distribution_steps SET state='cancelled' WHERE batch_id=?",(batch,))
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='distribution_materials'").fetchone():
                    db.execute('DELETE FROM distribution_materials WHERE batch_id=?',(batch,))
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='distribution_assets'").fetchone():
                    db.execute('DELETE FROM distribution_assets WHERE batch_id=?',(batch,))
            for step in item[0]['steps']:
                if step['platform']=='xhs' and step['receipt']:
                    with self.app.db_connection() as db:
                        row=db.execute("SELECT prepared FROM platform_xhs_receipts WHERE id=? AND status='verified'",(step['receipt'],)).fetchone()
                        db.execute("UPDATE platform_xhs_receipts SET status='cancelled' WHERE id=? AND status='verified'",(step['receipt'],))
                    if row:self.app.clear_manual_pending(json.loads(row[0])['history_id'],item[0]['profile'])
            self.set_batch(batch,'cancelled','已取消未提交的任务，官方草稿可手动保留或删除')
        return self.snapshot(batch_id=batch)

    def resolve(self,request):
        batch=request['id'];platform=request['platform'];state=request.get('state')
        if state not in ('published','scheduled','not_received'):raise ValueError('请选择实际核对结果')
        if request.get('confirmed') is not True:raise ValueError('必须先到官方作品管理核实实际结果')
        item=self.snapshot(batch_id=batch)['batches']
        if not item:raise ValueError('任务不存在')
        step=next((s for s in item[0]['steps'] if s['platform']==platform),None)
        if not step or step['state'] not in ('unknown','submitted'):raise ValueError('任务当前不需要结果确认')
        if state=='not_received':
            # Keep the old attempt record. Releasing it requires an explicit decision,
            # and applies only to this receipt, never all history for an account.
            with self.app.db_connection() as db:
                if platform=='xhs':
                    row=db.execute('SELECT prepared FROM platform_xhs_receipts WHERE id=?',(step['receipt'],)).fetchone()
                    db.execute("UPDATE platform_xhs_receipts SET status='not_received' WHERE id=?",(step['receipt'],))
                else:
                    db.execute('DELETE FROM platform_attempts WHERE receipt=?',(step['receipt'],))
                    db.execute("UPDATE platform_receipts SET status='not_received' WHERE id=?",(step['receipt'],))
            if platform=='xhs' and row:
                self.app.clear_manual_pending(json.loads(row[0])['history_id'],step['binding']['profile_dir'])
            self.set_step(batch,platform,'failed','用户已核实官方未接收；可重新核对继续')
        else:self.set_step(batch,platform,state,'用户已在官方作品管理确认：'+('已发布' if state=='published' else '定时待发布'))
        self.aggregate(batch)
        return self.snapshot(batch_id=batch)

    def reconcile(self,batch,platform):
        item=self.snapshot(batch_id=batch)['batches']
        if not item:raise ValueError('任务不存在')
        step=next((s for s in item[0]['steps'] if s['platform']==platform),None)
        if not step or step['state'] not in ('unknown','submitted','scheduled'):raise ValueError('此平台当前无需核实结果')
        with self.app.BROWSER_LOCK:
            self.check_binding(platform,step['binding'])
            if platform=='xhs':
                from datetime import datetime
                platform_xhs.init(self.app)
                with self.app.db_connection() as db:row=db.execute('SELECT prepared,attempted_at FROM platform_xhs_receipts WHERE id=?',(step['receipt'],)).fetchone()
                if not row or not row[1]:raise ValueError('缺少小红书实际提交时间，请人工到笔记管理核对')
                native=self.app.verify_history_published(json.loads(row[0])['history_id'],datetime.fromtimestamp(row[1]).isoformat())
                if native['status']=='unconfirmed':
                    from publish_checks import read_management,match_receipt
                    d=step['payload']['payload']
                    native=match_receipt(read_management(self.app.XHS_SKILL_DIR,published_only=True),{**d,'receipt_reference_time':datetime.fromtimestamp(row[1]).isoformat()})
                result={'status':{'published':'published','scheduled':'scheduled','reviewing':'submitted'}.get(native['status'],'unknown'),'message':native.get('reason','请到笔记管理确认最终状态'),'work_id':native.get('note_id',''),'url':native.get('url',''),'checked_at':time.time()}
            else:
                with self.app.db_connection() as db:
                    attempt=db.execute('SELECT created_at FROM platform_attempts WHERE receipt=?',(step['receipt'],)).fetchone()
                if not attempt:raise ValueError('缺少实际提交时间，请人工到官方作品管理核对')
                result=self.publisher.read_result(platform,step['payload'],attempt[0])
        with self.app.db_connection() as db:db.execute('INSERT OR REPLACE INTO distribution_results VALUES (?,?,?)',(batch,platform,json.dumps(result,ensure_ascii=False)))
        if result['status']!='unknown':self.set_step(batch,platform,result['status'],result['message'])
        self.aggregate(batch)
        return self.snapshot(batch_id=batch)

    def start(self):
        if self.thread and self.thread.is_alive():return
        def worker():
            while not self.stop.is_set():
                try:
                    with self.app.db_connection() as db:row=db.execute("SELECT id FROM distribution_batches WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
                    if row:self.execute(row[0]);continue
                except Exception as exc:
                    print('分发后台暂不可用：'+type(exc).__name__,flush=True)
                self.wake.wait(5);self.wake.clear()
        self.thread=threading.Thread(target=worker,name='distribution-worker',daemon=True);self.thread.start()
