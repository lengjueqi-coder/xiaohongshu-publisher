"""Daily file intake feeding durable multi-platform batches and native schedules."""
from __future__ import annotations
import copy
import hashlib
import json
import re
import threading
import time
import uuid
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import copy_quality
import vision_service
from platform_publishing import DECLARATIONS, file_stats, validate, TZ

DEFAULT={'root_path':'','targets':['douyin'],'daily_count':1,'times':['21:00'],'enabled':False,
         'use_vision':False,'copy_mode':'ai','mode':'review','tags':[],'platform_settings':{},'xhs_settings':{},'max_images':9}


def init_tables(db):
    db.execute('CREATE TABLE IF NOT EXISTS distribution_plans (profile TEXT PRIMARY KEY,config TEXT NOT NULL,error TEXT NOT NULL DEFAULT \'\',updated_at REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS distribution_materials (profile TEXT NOT NULL,fingerprint TEXT NOT NULL,source TEXT NOT NULL,batch_id TEXT NOT NULL,PRIMARY KEY(profile,fingerprint))')
    db.execute('CREATE TABLE IF NOT EXISTS distribution_slots (profile TEXT NOT NULL,slot TEXT NOT NULL,batch_id TEXT NOT NULL,PRIMARY KEY(profile,slot))')
    db.execute('CREATE TABLE IF NOT EXISTS distribution_assets (profile TEXT NOT NULL,sha256 TEXT NOT NULL,batch_id TEXT NOT NULL,PRIMARY KEY(profile,sha256))')


class DailyPlans:
    def __init__(self,app,distribution):
        self.app=app;self.distribution=distribution;self.lock=threading.RLock();self.thread=None;self.stop=threading.Event();self.hash_cache={}
        with app.db_connection() as db:init_tables(db)

    def get(self,profile):
        with self.app.db_connection() as db:row=db.execute('SELECT config,error FROM distribution_plans WHERE profile=?',(profile,)).fetchone()
        if row:return {'config':{**DEFAULT,**json.loads(row[0])},'error':row[1]}
        return {'config':copy.deepcopy(DEFAULT),'error':''}

    def save(self,profile,raw):
        with self.lock:
            previous=self.get(profile)['config']
            if previous['enabled']:raise ValueError('请先暂停每日分发计划再修改')
            c={**previous,**{k:v for k,v in raw.items() if k in DEFAULT},'enabled':False}
            root=Path(c['root_path']).expanduser().resolve()
            if not root.is_dir():raise ValueError('请选择可读取的图片或视频素材目录')
            c['root_path']=str(root);c['daily_count']=int(c['daily_count']);c['max_images']=int(c['max_images'])
            if c['daily_count'] not in (1,2):raise ValueError('每日分发限定 1～2 组')
            if not 1<=c['max_images']<=18:raise ValueError('每组图片数量须为 1～18')
            if not isinstance(c['targets'],list) or not c['targets'] or set(c['targets'])-{'xhs','douyin','channels'}:raise ValueError('请选择有效平台')
            c['targets']=list(dict.fromkeys(c['targets']))
            if c['mode'] not in ('review','automatic') or c['copy_mode'] not in ('ai','sidecar'):raise ValueError('运行或文案方式无效')
            c['use_vision']=c['use_vision'] is True
            if c['use_vision']:vision_service.validate_config(vision_service.config(self.app),True)
            if not isinstance(c['times'],list) or len(c['times'])!=c['daily_count']:raise ValueError('请为每日每一组设置一个北京时间')
            if any(not isinstance(t,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',t) for t in c['times']) or len(set(c['times']))!=len(c['times']):raise ValueError('发布时间格式须为 HH:MM，且不能重复')
            if not isinstance(c['tags'],list) or len(c['tags'])>(5 if 'douyin' in c['targets'] else 10):raise ValueError('话题过多；同步抖音时最多 5 个')
            if any(not isinstance(t,str) or re.search(r'[\s#@]',t) for t in c['tags']):raise ValueError('话题须逐项填写，不带空格、# 或 @')
            if not isinstance(c['platform_settings'],dict) or not isinstance(c['xhs_settings'],dict):raise ValueError('平台设置格式无效')
            # Each target must be connected before a rule can become executable.
            for target in c['targets']:
                if target!='xhs' and not self.distribution.publisher.binding(target).get('account_id'):raise ValueError('请先连接'+target+'账号')
            if 'xhs' in c['targets']:
                with self.app.db_connection() as db:
                    row=db.execute('SELECT enabled FROM automation_plans WHERE profile_dir=?',(profile,)).fetchone()
                if row and row[0]:raise ValueError('请先暂停旧的小红书自动计划，避免与多平台每日计划重叠')
            with self.app.db_connection() as db:db.execute('INSERT INTO distribution_plans VALUES (?,?,?,?) ON CONFLICT(profile) DO UPDATE SET config=excluded.config,error=excluded.error,updated_at=excluded.updated_at',(profile,json.dumps(c,ensure_ascii=False),'',time.time()))
            return self.get(profile)

    def enabled(self,profile,enabled):
        with self.lock:
            c=self.get(profile)['config']
            if enabled:
                # Revalidate stored values, especially disconnected disks and API keys.
                self.save(profile,c);c=self.get(profile)['config']
                if c['copy_mode']=='ai' and not self.app.profile_prompt_block(profile) and not any(self.app.db_get_setting(f'distribution_persona:{profile}:{target}',{}).get('brief','').strip() for target in c['targets']):raise ValueError('请先保存并确认账号画像')
                for target in c['targets']:
                    if target!='xhs':self.distribution.publisher.page(target)
                if c['copy_mode']=='ai' and not c['use_vision']:
                    model=self.app.text_model_config(profile)
                    if not model.get('api_key'):raise ValueError('AI 文案模型尚未配置；也可使用每组的发布文案.json')
            c['enabled']=bool(enabled)
            with self.app.db_connection() as db:
                db.execute('BEGIN IMMEDIATE')
                if enabled and 'xhs' in c['targets']:
                    other=db.execute('SELECT enabled FROM automation_plans WHERE profile_dir=?',(profile,)).fetchone()
                    if other and other[0]:raise ValueError('请先暂停旧的小红书计划')
                db.execute('UPDATE distribution_plans SET config=?,error=?,updated_at=? WHERE profile=?',(json.dumps(c,ensure_ascii=False),'',time.time(),profile))
            return self.get(profile)

    def scan(self,profile):
        c=self.get(profile)['config'];root=Path(c['root_path'])
        if not root.is_dir():raise ValueError('素材目录不可用，请连接磁盘')
        groups=[];now=time.time();seen=0
        with self.app.db_connection() as db:
            used={r[0] for r in db.execute('SELECT fingerprint FROM distribution_materials WHERE profile=?',(profile,))}
            used_assets={r[0] for r in db.execute('SELECT sha256 FROM distribution_assets WHERE profile=?',(profile,))}
        folders=[root]
        for p in root.rglob('*'):
            if p.is_dir() and not p.is_symlink() and not any(x.startswith('.') for x in p.relative_to(root).parts):folders.append(p)
            if len(folders)>5000:raise ValueError('素材目录超过5000个，请缩小根目录范围')
        folders=sorted(folders)
        def asset(p):
            s=p.stat();key=(str(p),s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_ino)
            if key not in self.hash_cache:
                if len(self.hash_cache)>20000:self.hash_cache.clear()
                self.hash_cache[key]=file_stats([str(p)])[0]
            return self.hash_cache[key]
        for folder in folders:
            if any(x in folder.parts for x in ('已发布','已使用','archive')):continue
            files=sorted([p for p in folder.iterdir() if p.is_file() and not p.is_symlink() and not p.name.startswith('.')])
            images=[p for p in files if p.suffix.lower() in vision_service.IMAGE_TYPES]
            videos=[p for p in files if p.suffix.lower() in vision_service.VIDEO_TYPES]
            # Hash each asset once. Renaming/moving an already selected source must
            # not republish it, and excess photos stay available for later days.
            stats={str(p):asset(p) for p in images+videos if now-p.stat().st_mtime>=60}
            images=[p for p in images if str(p) in stats and stats[str(p)][3] not in used_assets]
            videos=[p for p in videos if str(p) in stats and stats[str(p)][3] not in used_assets]
            maximum=min(c['max_images'],18)
            candidates=[('image',images[i:i+maximum]) for i in range(0,len(images),maximum)]+[('video',[p]) for p in videos]
            for kind,media in candidates:
                seen+=1
                if seen>5000:raise ValueError('素材组超过 5000，请缩小根目录范围')
                if any(now-p.stat().st_mtime<60 for p in media):continue
                hashes=[stats[str(p)][3] for p in media]
                fingerprint=hashlib.sha256(json.dumps([kind,hashes]).encode()).hexdigest()
                if fingerprint in used:continue
                groups.append({'folder':str(folder),'files':[str(p) for p in media],'hashes':hashes,'kind':kind,'fingerprint':fingerprint,'name':folder.name if kind=='image' else media[0].stem})
        return groups

    def build(self,profile,slot=None,automatic=False):
        with self.lock:
            c=self.get(profile)['config'];groups=self.scan(profile)
            if not groups:raise ValueError('暂无未使用且已保存完成的素材组')
            group=groups[0];folder=Path(group['folder'])
            with self.app.db_connection() as db:
                recent=[json.loads(r[0]) for r in db.execute("SELECT payload FROM distribution_steps s JOIN distribution_batches b ON b.id=s.batch_id WHERE b.profile=? AND b.state!='cancelled' ORDER BY b.created_at DESC LIMIT 60",(profile,))]
            recent=[d.get('payload',d) for d in recent]
            recent_copies=list({(p.get('title',''),p.get('content','')):p for p in recent}.values())
            notes=copy_quality.material_notes(folder)
            sidecar=folder/'发布文案.json'
            if c['copy_mode']=='sidecar':
                if not sidecar.is_file() or sidecar.stat().st_size>50000:raise ValueError('本组缺少小于 50KB 的发布文案.json，需包含 title、content')
                content=json.loads(sidecar.read_text('utf-8-sig'))
            else:content=vision_service.generate(self.app,{'profile_dir':profile,'platform':c['targets'][0],'platforms':c['targets'],'files':group['files'],'use_vision':c['use_vision'],'notes':notes,'tags':c['tags'],'recent_copies':recent_copies[:8]})
            title=str(content.get('title',''));body=str(content.get('content',''))
            if not title or not body or len(title)>20 or len(body)>1000:raise ValueError('文案缺失或超限，未创建任务')
            copy_quality.ensure_distinct({'title':title,'content':body},[{'title':p.get('title',''),'content':p.get('content','')} for p in recent])
            targets={}
            for platform in c['targets']:
                if platform=='xhs':
                    d={**c['xhs_settings'],'profile_dir':profile,'title':title,'content':body,'tags':c['tags'],'images':group['files'],'publish_mode':'scheduled' if slot else 'immediate','schedule_time':slot or '','timezone':'Asia/Shanghai','schedule_window':'','visibility':c['xhs_settings'].get('visibility','公开可见')}
                    if group['kind']=='video':d['video_path']=group['files'][0];d['images']=[]
                    targets[platform]={'media_type':group['kind'],'payload':d}
                else:
                    settings=copy.deepcopy(c['platform_settings'].get(platform,{}))
                    settings.pop('platform',None);settings.pop('media_type',None);settings.pop('files',None)
                    if isinstance(settings.get('shoot_region'),str):settings['shoot_region']=[x.strip() for x in settings['shoot_region'].split('/') if x.strip()]
                    if group['kind']=='video':settings.pop('music',None)
                    if platform=='channels':settings.pop('allow_download',None);settings.pop('hotspot',None)
                    d={**settings,'platform':platform,'media_type':group['kind'],'title':title,'content':body,'tags':c['tags'],'files':group['files'],'declaration':settings.get('declaration',DECLARATIONS[platform][0]),'visibility':settings.get('visibility','public'),'publish_mode':'scheduled' if slot else 'immediate','schedule_time':slot or ''}
                    if platform=='channels' and group['kind']=='video':d['short_title']=str(content.get('short_title',''))
                    targets[platform]=validate(d)
            request_id='daily:'+profile+':'+slot if slot else 'preview:'+profile+':'+uuid.uuid4().hex
            result=self.distribution.create({'profile_dir':profile,'targets':targets,'request_id':request_id,'source_key':group['fingerprint'],'paused':not automatic},reservation={**group,'slot':slot})
            batch=result['batches'][0]['id']
            # Review drafts remain paused until explicitly approved by the operator.
            if not automatic:self.distribution.set_batch(batch,'paused','预览已制作；尚未上传或发布，确认后可继续')
            return self.distribution.snapshot(batch_id=batch)

    def tick(self,now=None):
        now=now or datetime.now(TZ)
        with self.app.db_connection() as db:profiles=[(r[0],json.loads(r[1])) for r in db.execute('SELECT profile,config FROM distribution_plans')]
        for profile,c in profiles:
            if not c.get('enabled'):continue
            # Native schedule is filled ahead of time. Mac need not stay awake at the
            # final publication instant after the platform accepts the schedule.
            for days in (0,1):
                for hhmm in c['times']:
                    hour,minute=map(int,hhmm.split(':'));when=(now+timedelta(days=days)).replace(hour=hour,minute=minute,second=0,microsecond=0)
                    delta=(when-now).total_seconds()
                    if not 2*3600+15*60<=delta<=26*3600:continue
                    slot=when.strftime('%Y-%m-%d %H:%M')
                    with self.app.db_connection() as db:exists=db.execute('SELECT 1 FROM distribution_slots WHERE profile=? AND slot=?',(profile,slot)).fetchone()
                    if exists:continue
                    try:self.build(profile,slot,automatic=c['mode']=='automatic')
                    except Exception as exc:
                        message=str(exc)
                        if not message.startswith('暂无未使用'):
                            c['enabled']=False
                        with self.app.db_connection() as db:db.execute('UPDATE distribution_plans SET config=?,error=?,updated_at=? WHERE profile=?',(json.dumps(c,ensure_ascii=False),message[:1000],time.time(),profile))
                        break

    def start(self):
        if self.thread and self.thread.is_alive():return
        def run():
            while not self.stop.is_set():
                try:self.tick()
                except Exception:pass
                self.stop.wait(30)
        self.thread=threading.Thread(target=run,name='daily-distribution',daemon=True);self.thread.start()
