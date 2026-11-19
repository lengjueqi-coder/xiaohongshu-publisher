"""Douyin / WeChat Channels: prepare, read back, then submit a single-use receipt."""
from __future__ import annotations
import base64
import hashlib
import json
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from platform_bridge import Page, URLS, rpc, tabs

NAMES={'douyin':'抖音','channels':'视频号'}
TZ=ZoneInfo('Asia/Shanghai')
DECLARATIONS={
 'douyin':['无需添加自主声明','内容由AI生成','内容为个人观点或见解','内容为转载信息','内容含营销推广信息','虚构演绎，仅供娱乐'],
 'channels':['无需标注','含AI生成内容','内容为虚构剧情，仅供娱乐','个人观点，仅供参考','内容包含营销广告','内容为自行拍摄','内容为转载'],
}
FIELDS={'platform','media_type','title','short_title','content','tags','files','visibility','allow_download','publish_mode','schedule_time','declaration','location','collection','activity','music','hotspot','native_settings','shoot_date','shoot_region','repost_source'}
EMPTY_OPTIONS={'collection':'不选择合集','location':'不显示位置','activity':'不参与活动','music':'不添加音乐','hotspot':'不关联热点'}


def option_fields(platform, kind):
    return ['collection','location','activity']+(['music'] if kind=='image' else [])+(['hotspot'] if platform=='douyin' else [])


def canon(value):
    return '\n'.join(re.sub(r'[ \t\u00a0]+',' ',line).strip() for line in str(value or '').replace('\r\n','\n').replace('\u200b','').replace('\ufeff','').split('\n')).strip()


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def validate(payload, now=None):
    if not isinstance(payload,dict):raise ValueError('发布内容格式错误')
    extra=set(payload)-FIELDS
    if extra:raise ValueError('尚未支持的发布设置：'+','.join(sorted(extra)))
    d=dict(payload)
    platform=d.get('platform'); media=d.get('media_type')
    if platform not in NAMES or media not in ('image','video'):raise ValueError('请选择抖音或视频号的图文/视频')
    for key in ('title','short_title','content','schedule_time','declaration','location','collection','activity','music','hotspot','shoot_date','repost_source'):
        if key in d and not isinstance(d[key],str):raise ValueError('字段必须为文字：'+key)
    supported=option_fields(platform,media)
    for key,empty in EMPTY_OPTIONS.items():
        if key in supported:d[key]=d.get(key) or empty
        elif d.get(key) not in (None,'',empty):raise ValueError('当前发布类型没有此官方设置：'+key)
        else:d.pop(key,None)
    title=d.get('title',''); short=d.get('short_title',''); content=d.get('content','')
    max_title=(20 if media=='image' else 30) if platform=='douyin' else (22 if media=='image' else 30)
    if len(title)>max_title:raise ValueError(f'{NAMES[platform]}标题不能超过 {max_title} 个字符，软件不会自动截断')
    if platform=='channels' and media=='video' and short and not 6<=len(short)<=16:raise ValueError('视频号短标题请填写 6～16 个字符，或留空')
    tags=d.get('tags',[])
    if not isinstance(tags,list) or any(not isinstance(t,str) or not t or t!=t.strip() or re.search(r'[\s#@]',t) for t in tags):raise ValueError('话题请逐个填写，不带空格、# 或 @')
    if len(tags)!=len(set(tags)):raise ValueError('话题重复，请先去重')
    if len(tags)+(1 if d.get('activity') and d['activity']!='不参与活动' else 0)>5 and platform=='douyin':raise ValueError('抖音话题最多 5 个')
    if len(tags)>10:raise ValueError('每条内容最多填写 10 个话题')
    full=content+''.join(' #'+t for t in tags)
    if len(full)>1000:raise ValueError('正文与话题合计不能超过 1000 个字符，软件不会自动截断')
    if not title and not content:raise ValueError('请填写标题或正文')
    if d.get('visibility','public') not in ('public','friends','private'):raise ValueError('可见范围无效')
    if platform=='channels' and d.get('visibility','public')!='public':raise ValueError('当前视频号官方网页没有私密/好友可见选项，已阻止提交')
    if 'allow_download' in d and type(d['allow_download']) is not bool:raise ValueError('保存权限格式错误')
    if d.get('declaration') not in DECLARATIONS[platform]:raise ValueError('请选择官方内容声明')
    if platform=='channels' and d['declaration']=='内容为自行拍摄':
        try:shoot=datetime.strptime(d.get('shoot_date',''),'%Y-%m-%d').date()
        except ValueError:raise ValueError('请填写有效的拍摄日期') from None
        if shoot>datetime.now(TZ).date():raise ValueError('拍摄日期不能晚于今天')
        region=d.get('shoot_region')
        if not isinstance(region,list) or not region or any(not isinstance(x,str) or not x.strip() for x in region):raise ValueError('请填写官方拍摄地点路径，如 中国 / 北京')
    files=d.get('files',[])
    if not isinstance(files,list) or not files:raise ValueError('请选择素材')
    if len(files)>(35 if platform=='douyin' else 18) or (media=='video' and len(files)!=1):raise ValueError('素材数量不符合当前平台要求')
    allowed={'.jpg','.jpeg','.png','.webp','.bmp','.tif'} if media=='image' else {'.mp4','.mov','.m4v','.webm'}
    max_bytes=(50 if platform=='douyin' else 20)*1024**2 if media=='image' else (64 if platform=='douyin' else 20)*1024**3
    d['files']=[]
    for raw in files:
        if not isinstance(raw,str):raise ValueError('素材路径无效')
        f=Path(raw).expanduser().resolve()
        if not f.is_file() or f.suffix.lower() not in allowed:raise ValueError('素材不存在或类型不支持：'+f.name)
        if not 0<f.stat().st_size<=max_bytes:raise ValueError('素材大小不符合平台要求：'+f.name)
        d['files'].append(str(f))
    mode=d.get('publish_mode','immediate')
    if mode not in ('immediate','scheduled'):raise ValueError('发布方式无效')
    if mode=='scheduled' and platform=='douyin' and d.get('visibility','public')!='public':raise ValueError('抖音选择定时会恢复为公开；私密或好友可见请使用立即发布')
    if mode=='scheduled':
        try:when=datetime.strptime(d.get('schedule_time','').replace('T',' '),'%Y-%m-%d %H:%M').replace(tzinfo=TZ)
        except ValueError:raise ValueError('定时请填写北京时间，精确到分钟') from None
        current=now or datetime.now(TZ)
        if not current+timedelta(hours=2)<=when<=current+timedelta(days=14):raise ValueError('定时请选择北京时间 2 小时后、14 天内；上传后会再次校验')
        d['schedule_time']=when.strftime('%Y-%m-%d %H:%M')
    elif d.get('schedule_time'):raise ValueError('立即发布时不应保留定时时间')
    if not isinstance(d.get('native_settings',{}),dict):raise ValueError('官方设置快照格式错误')
    if d.get('native_settings'):raise ValueError('未识别的官方附加设置，请在官方页面核对')
    return d


def compare(payload, snapshot, expected_media=None):
    checks=[]
    def check(key,expected,actual):
        checks.append({'field':key,'expected':expected,'actual':actual,'ok':expected==actual})
    f=snapshot.get('fields',{});p=payload['platform'];kind=payload['media_type']
    check('发布类型',kind,snapshot.get('media_type'))
    if p=='douyin' or kind=='image':check('标题',payload.get('title',''),f.get('title'))
    else:check('短标题',payload.get('short_title',''),f.get('short_title'))
    expected=payload.get('content','')+''.join(' #'+t for t in payload.get('tags',[]))
    check('正文与换行',canon(expected),canon(f.get('content')))
    check('已识别话题',['#'+t for t in payload.get('tags',[])],f.get('tags',[]))
    check('内容声明',payload['declaration'],f.get('declaration'))
    if p=='channels' and payload['declaration']=='内容为自行拍摄':
        detail=f.get('declaration_detail','')
        check('拍摄日期',True,payload['shoot_date'] in detail)
        check('拍摄地点',True,all(x in detail for x in payload['shoot_region'] if x!='中国'))
    if p=='channels' and payload['declaration']=='内容为转载' and payload.get('repost_source'):
        check('转载来源',True,payload['repost_source'] in f.get('declaration_detail',''))
    if kind=='image':
        check('图片数量',len(payload['files']),snapshot.get('image_count'))
        check('图片处理完成',True,snapshot.get('media_ready',False))
    else:
        check('视频就绪',True,bool(snapshot.get('video') and snapshot['video'].get('src')))
        duration=(snapshot.get('video') or {}).get('duration')
        check('视频时长符合要求',True,isinstance(duration,(int,float)) and 0<duration<=(3 if p=='douyin' else 8)*3600)
    if expected_media is not None:check('素材未被替换',True,expected_media==media_stamp(snapshot))
    radio=snapshot.get('radios',[])
    if p=='douyin':
        label={'public':'公开','friends':'好友可见','private':'仅自己可见'}[payload.get('visibility','public')]
        check('谁可以看',label,next((x for x in radio if x in ['公开','好友可见','仅自己可见']),None))
        check('保存权限','允许' if payload.get('allow_download',True) else '不允许',next((x for x in radio if x in ['允许','不允许']),None))
        check('平台附加分发','不同时发布',next((x for x in radio if x in ['不同时发布','同时发布到']),None))
    scheduled=payload.get('publish_mode')=='scheduled'
    selected=('定时发布' if scheduled else '立即发布') if p=='douyin' else ('定时' if scheduled else '不定时')
    check('发布方式',True,selected in radio)
    if scheduled:check('北京时间',payload['schedule_time'],f.get('schedule_time'))
    for key,label in [('location','位置'),('collection','合集'),('activity','活动'),('music','音乐'),('hotspot','热点')]:
        if key in payload and payload[key]!='':check(label,payload[key],f.get(key))
    check('官方错误提示',[],snapshot.get('errors',[]))
    check('登录与安全验证正常',False,bool(snapshot.get('risky')))
    return checks


def media_stamp(snapshot):
    return {'images':snapshot.get('media',[]),'video':(snapshot.get('video') or {}).get('src','')}


class Publisher:
    def __init__(self, app):
        self.app=app
        self._init_lock=threading.Lock()
        self._ready=False
        self.uploads={}

    def init(self):
        with self._init_lock:
            if self._ready:return
            with self.app.db_connection() as db:
                db.execute('CREATE TABLE IF NOT EXISTS platform_bindings (platform TEXT PRIMARY KEY, binding TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS platform_uploads (platform TEXT NOT NULL, tab_id INTEGER NOT NULL, manifest TEXT NOT NULL, PRIMARY KEY(platform,tab_id))')
                db.execute('CREATE TABLE IF NOT EXISTS platform_attempts (fingerprint TEXT PRIMARY KEY, receipt TEXT NOT NULL, created_at REAL NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS platform_receipts (id TEXT PRIMARY KEY, platform TEXT NOT NULL, payload TEXT NOT NULL, binding TEXT NOT NULL, checks TEXT NOT NULL, media TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, message TEXT NOT NULL DEFAULT \'\')')
                for platform,tab,manifest in db.execute('SELECT platform,tab_id,manifest FROM platform_uploads'):
                    self.uploads[(platform,tab)]=json.loads(manifest)
            self._ready=True

    def binding(self, platform):
        self.init()
        with self.app.db_connection() as db:row=db.execute('SELECT binding FROM platform_bindings WHERE platform=?',(platform,)).fetchone()
        if not row:raise ValueError('请先连接'+NAMES.get(platform,platform)+'官方页面')
        return json.loads(row[0])

    def bind(self, request):
        platform=request.get('platform'); client=request.get('client_id'); tab=request.get('tab_id')
        if platform not in URLS:raise ValueError('平台无效')
        available=tabs()
        if not any(t['id']==tab and t['client_id']==client and t['platform']==platform for t in available):raise ValueError('所选官方标签页已失效，请刷新列表')
        binding={'platform':platform,'client_id':client,'tab_id':int(tab)}
        p=Page(binding);snapshot=p.snapshot()
        for attempt in range(30):
            if snapshot.get('account_id') and snapshot.get('account'):break
            time.sleep(.4)
            p=Page(binding);snapshot=p.snapshot()
        binding['account']=snapshot.get('account','')
        binding['account_id']=snapshot.get('account_id','')
        if not binding['account_id'] or not binding['account']:
            raise ValueError('无法识别官方账号，请等待登录完成后重新连接')
        self.init()
        with self.app.db_connection() as db:
            db.execute('INSERT OR REPLACE INTO platform_bindings VALUES (?,?)',(platform,json.dumps(binding)))
            db.execute("UPDATE platform_receipts SET status='invalidated' WHERE platform=? AND status='verified'",(platform,))
        return {'binding':binding,'snapshot':public_snapshot(snapshot)}

    def page(self, platform):
        binding=self.binding(platform);page=Page(binding)
        current=page.snapshot()
        if current.get('risky'):raise ValueError('官方页面要求登录/安全验证，请在 Chrome 中处理后再继续')
        if binding.get('account') and current.get('account') and current['account']!=binding['account']:raise ValueError('官方页面账号已经变化，请重新连接确认')
        if not binding.get('account_id') or not current.get('account_id'):
            raise ValueError('账号身份未核实，请重新连接官方账号')
        if binding['account_id']!=current['account_id']:
            raise ValueError('官方页面账号已经变化，请重新连接确认')
        if current.get('ready') and not current.get('account'):raise ValueError('无法识别官方发布页的当前账号，请重新连接确认')
        if 'login' in current['url']:raise ValueError('官方登录已过期，请在 Chrome 扫码登录')
        return page

    def open(self, request):
        platform=request.get('platform');kind=request.get('media_type','image')
        if platform not in URLS or kind not in URLS[platform]:raise ValueError('平台/类型无效')
        available=tabs();clients=list(dict.fromkeys(t['client_id'] for t in available))
        client=request.get('client_id') or (clients[0] if len(clients)==1 else '')
        if not client:raise ValueError('请先选择 Chrome 中的一个官方标签页')
        if platform=='channels':
            try:binding=self.binding(platform)
            except ValueError:binding=None
            if binding and binding['client_id']==client:
                try:page=self.page(platform)
                except RuntimeError as exc:
                    if not any(x in str(exc).lower() for x in ('no tab','tab not found','标签页已','invalid tab')):raise
                    page=None
                current=page.snapshot() if page else None
                if current and not current['image_count'] and not current.get('video') and not any((current.get('fields',{}).get(k) or '').strip() for k in ('content','title','short_title')):
                    # Keep the official SPA session when it is safe to reuse an
                    # empty form or manager. A fresh tab may require a QR login.
                    from urllib.parse import urlsplit
                    page.dom('navigate_empty_channels',path=urlsplit(URLS[platform][kind]).path)
                    return self.bind(binding)
        new=rpc(client,platform,'open',url=URLS[platform][kind]);time.sleep(.5)
        return self.bind({'platform':platform,'client_id':client,'tab_id':new['id']})

    def upload(self, request):
        platform=request['platform'];kind=request['media_type'];files=request['files']
        # Reuse strict media validation without requiring a finished caption.
        checked=validate(dict(platform=platform,media_type=kind,files=files,title='上传',content='',tags=[],declaration=DECLARATIONS[platform][0]))
        page=self.page(platform);snapshot=page.snapshot()
        old=self.uploads.get((platform,page.tab_id))
        resume=bool(old and old['files']==checked['files'] and old['stats']==file_stats(checked['files']) and pending_upload(old))
        if not resume and snapshot['ready'] and (snapshot['image_count'] or snapshot.get('video')):raise ValueError('当前官方页面已有素材。请点击“新建发布页”后再上传，避免覆盖现有草稿')
        page.call('focus')
        prefix="document.querySelector('wujie-app').shadowRoot" if platform=='channels' else 'document'
        expr=prefix+f'.querySelector(\'input[type="file"][accept*="{kind}"]\')'
        if not resume:
            for attempt in range(30):
                try:
                    if page.call('evaluate',expression='Boolean('+expr+')'):
                        break
                except RuntimeError:
                    pass
                time.sleep(.5)
            page.call('files',expression=expr,files=checked['files'])
            self.uploads[(platform,page.tab_id)]={'files':checked['files'],'stats':file_stats(checked['files']),'media':{},'pending':True}
            with self.app.db_connection() as db:db.execute('INSERT OR REPLACE INTO platform_uploads VALUES (?,?,?)',(platform,page.tab_id,json.dumps(self.uploads[(platform,page.tab_id)])))
        end=time.monotonic()+300
        stable=0;previous=None
        while time.monotonic()<end:
            time.sleep(1)
            s=page.snapshot()
            if s.get('risky') or s.get('errors'):raise ValueError('官方上传提示：'+'；'.join(s.get('errors') or ['请先处理安全验证']))
            media=media_stamp(s)
            complete=s['ready'] and ((kind=='image' and s['image_count']==len(files) and s.get('media_ready')) or (kind=='video' and s.get('video') and s['video'].get('src') and s['video'].get('duration')))
            stable=stable+1 if complete and previous==media else 0
            previous=media
            if stable>=3:
                self.uploads[(platform,page.tab_id)]={'files':checked['files'],'stats':file_stats(checked['files']),'media':media_stamp(s)}
                binding=self.binding(platform)
                if s.get('account') and not binding.get('account'):
                    binding['account']=s['account']
                    with self.app.db_connection() as db:db.execute('UPDATE platform_bindings SET binding=? WHERE platform=?',(json.dumps(binding),platform))
                with self.app.db_connection() as db:db.execute('INSERT OR REPLACE INTO platform_uploads VALUES (?,?,?)',(platform,page.tab_id,json.dumps(self.uploads[(platform,page.tab_id)])))
                return {'snapshot':public_snapshot(s),'files':checked['files'],'binding':self.binding(platform)}
        raise ValueError('素材上传尚未完成，请查看官方页面；软件不会继续点击发布')

    def fill(self, request):
        d=validate(request);platform=d['platform'];page=self.page(platform);page.call('focus')
        before=page.snapshot()
        if not before['ready']:raise ValueError('请先上传素材，等待官方发布表单加载')
        if before['media_type']!=d['media_type']:raise ValueError('官方页面的图文/视频类型与工作台不一致，请新建对应发布页')
        uploaded=self.uploads.get((platform,page.tab_id))
        if not uploaded or uploaded['files']!=d['files'] or uploaded['stats']!=file_stats(d['files']):
            raise ValueError('素材没有由本工作台核验上传。请新建发布页并通过工作台上传一次')
        if uploaded['media']!=media_stamp(before):raise ValueError('官方页面素材发生变化，请重新上传核验')
        if platform=='douyin' or d['media_type']=='image':page.text('title',d.get('title',''))
        else:page.text('short_title',d.get('short_title',''))
        page.editor(d.get('content',''),d.get('tags',[]))
        page.declaration(d)
        if platform=='douyin':
            page.radio({'public':'公开','friends':'好友可见','private':'仅自己可见'}[d.get('visibility','public')])
            page.radio('允许' if d.get('allow_download',True) else '不允许')
            page.radio('不同时发布')
        for field in ('collection','location','activity','music','hotspot'):
            value=d.get(field)
            if value and page.dom('read_field',field=field)!=value:
                page.choose(field,value)
        if d.get('publish_mode')=='scheduled':page.schedule(d['schedule_time'])
        else:page.radio('立即发布' if platform=='douyin' else '不定时')
        time.sleep(.4)
        return self.verify(d)

    def prepare(self, request):
        """Create/upload only when needed, preserving previously verified drafts."""
        d=validate(request);platform=d['platform']
        try:binding=self.binding(platform)
        except ValueError:binding=None
        ready=False
        if binding:
            self.check_attempt(d,binding)
            try:page=self.page(platform)
            except RuntimeError as exc:
                if not any(x in str(exc).lower() for x in ('no tab','tab not found','标签页已','invalid tab')):raise
                page=None
            if page is None:
                self.open(dict(platform=platform,media_type=d['media_type'],client_id=binding['client_id']))
                page=self.page(platform)
            manifest=self.uploads.get((platform,page.tab_id))
            if manifest and manifest['files']==d['files'] and manifest['stats']==file_stats(d['files']) and pending_upload(manifest):
                self.upload(dict(platform=platform,media_type=d['media_type'],files=d['files']))
                manifest=self.uploads[(platform,page.tab_id)]
            snapshot=page.snapshot()
            ready=bool(manifest and manifest['files']==d['files'] and manifest['stats']==file_stats(d['files']) and manifest['media']==media_stamp(snapshot))
            if ready and all(c['ok'] for c in compare(d,snapshot,manifest['media'])):
                result=self.verify(d);result['binding']=self.binding(platform);return result
            # Rebuild a changed nonempty editor in a fresh official draft. Different
            # native editor versions handle select-all differently; never append.
            if ready and snapshot.get('fields',{}).get('content'):ready=False
        if not ready:
            self.open(dict(platform=platform,media_type=d['media_type'],client_id=(binding or {}).get('client_id')))
            self.upload(dict(platform=platform,media_type=d['media_type'],files=d['files']))
        result=self.fill(d)
        result['binding']=self.binding(platform)
        return result

    def submission_key(self, payload, binding):
        # Same content in a new tab must not bypass an uncertain submission.
        effective=dict(payload)
        if payload['platform']=='douyin' or payload['media_type']=='image':effective.pop('short_title',None)
        if payload['platform']=='channels' and payload['media_type']=='video':effective.pop('title',None)
        effective.pop('native_settings',None)
        effective.pop('files',None)
        return digest({'platform':payload['platform'],'account':binding.get('account_id') or binding.get('account',''),'payload':effective,'files':[s[-1] for s in file_stats(payload['files'])]})

    def check_attempt(self, payload, binding):
        with self.app.db_connection() as db:
            row=db.execute('SELECT receipt FROM platform_attempts WHERE fingerprint=?',(self.submission_key(payload,binding),)).fetchone()
        if row:raise ValueError('这份内容已经尝试提交过，请到官方作品管理核对；不会整批重发')

    def verify(self, request):
        d=validate(request);page=self.page(d['platform']);snapshot=page.snapshot()
        uploaded=self.uploads.get((d['platform'],page.tab_id))
        if not uploaded or uploaded['files']!=d['files'] or uploaded['stats']!=file_stats(d['files']):raise ValueError('素材上传记录失效，请重新上传')
        checks=compare(d,snapshot,uploaded['media'])
        ok=all(c['ok'] for c in checks)
        token=uuid.uuid4().hex if ok else ''
        self.init()
        with self.app.db_connection() as db:
            db.execute("UPDATE platform_receipts SET status='invalidated' WHERE platform=? AND status='verified'",(d['platform'],))
            if ok:
                db.execute('INSERT INTO platform_receipts (id,platform,payload,binding,checks,media,status,created_at) VALUES (?,?,?,?,?,?,?,?)',(token,d['platform'],json.dumps(d,ensure_ascii=False),json.dumps(self.binding(d['platform'])),json.dumps(checks,ensure_ascii=False),json.dumps(uploaded['media']),'verified',time.time()))
        return {'verified':ok,'receipt':token,'checks':checks,'snapshot':public_snapshot(snapshot)}

    def submit(self, request):
        token=request.get('receipt','')
        self.init()
        with self.app.db_connection() as db:
            row=db.execute('SELECT platform,payload,binding,media,status,created_at FROM platform_receipts WHERE id=?',(token,)).fetchone()
        if not row or row[4]!='verified':raise ValueError('校验凭证已用过或失效，请重新核对；不会自动重试发布')
        if time.time()-row[5]>1800:raise ValueError('校验已超过 30 分钟，请重新核对')
        d=validate(json.loads(row[1]));binding=json.loads(row[2]);page=self.page(row[0])
        uploaded=self.uploads.get((d['platform'],page.tab_id))
        if not uploaded or uploaded['files']!=d['files'] or uploaded['stats']!=file_stats(d['files']):
            raise ValueError('素材在核对后发生变化，请重新上传核对')
        if self.binding(row[0])!=binding:raise ValueError('连接已变化，请重新核对')
        if request.get('payload_digest') and request['payload_digest']!=digest(d):raise ValueError('文案或设置已修改，请重新填写核验')
        page.call('focus');s=page.snapshot();checks=compare(d,s,json.loads(row[3]))
        if not all(c['ok'] for c in checks):raise ValueError('官方页面在校验后发生变化，已阻止提交：'+','.join(c['field'] for c in checks if not c['ok']))
        # Claim BEFORE the click. If the process dies after clicking, never submit again.
        with self.app.db_connection() as db:
            fingerprint=self.submission_key(d,binding)
            claimed=db.execute('INSERT OR IGNORE INTO platform_attempts VALUES (?,?,?)',(fingerprint,token,time.time())).rowcount
            if claimed!=1:raise ValueError('这份内容已经尝试提交过。请查看官方作品管理，重新核对也不会重复发布')
            changed=db.execute("UPDATE platform_receipts SET status='submitting' WHERE id=? AND status='verified'",(token,)).rowcount
        if changed!=1:raise ValueError('该内容已经提交过，请查看发布记录')
        try:
            page.click(button='发布' if row[0]=='douyin' else '发表')
            end=time.monotonic()+25
            last=s
            while time.monotonic()<end:
                time.sleep(1)
                last=page.snapshot()
                if last.get('risky'):
                    status='unknown';message='官方要求本人安全验证，请在 Chrome 完成并核对作品；软件不会自动重试';break
                if ('/content/manage' in last['url'] or (row[0]=='channels' and ('/post/list' in last['url'] or '/finderNewLifePostList' in last['url']))):
                    status='submitted';message='官方页面已跳转到作品管理；发布/审核状态以官方列表为准';break
                if last.get('errors'):status='unknown';message='官方返回：'+'；'.join(last['errors']);break
            else:status='unknown';message='已经点击一次发布，尚未确认结果。请到官方页面核对，勿重复提交'
        except Exception as exc:
            status='unknown';message='提交结果待核对：'+str(exc)
        with self.app.db_connection() as db:db.execute('UPDATE platform_receipts SET status=?,message=? WHERE id=?',(status,message,token))
        return {'status':status,'message':message,'receipt':token}

    def options(self, request):
        field=request.get('field');platform=request['platform']
        if field not in ('location','collection','activity','music','hotspot'):raise ValueError('不支持的选项')
        page=self.page(platform);page.call('focus')
        if platform=='douyin' and page.dom('close_music_picker'):
            time.sleep(.4)
        if not (platform=='douyin' and field=='activity'):
            page.click(field=field);time.sleep(.5)
        query=str(request.get('query','')).strip()
        if query:
            page.dom('search_option',field=field,value=query)
            if platform=='douyin' and field=='music':page.call('key',key='Enter')
            time.sleep(.8)
        options=page.dom('options',field=field)
        if query or field=='music':
            for _ in range(10):
                if any(o['label']!=EMPTY_OPTIONS[field] for o in options):break
                time.sleep(.25)
                options=page.dom('options',field=field)
        empty=EMPTY_OPTIONS[field]
        if not any(o['label']==empty for o in options):options.insert(0,{'label':empty,'detail':empty})
        if platform=='douyin' and field=='music' and page.dom('close_music_picker'):
            time.sleep(.4)
        page.call('key',key='Escape')
        page.click(field='title' if page.dom('read_field',field='title') is not None else 'short_title')
        return {'options':options}

    def history(self):
        self.init()
        with self.app.db_connection() as db:
            rows=db.execute("SELECT id,platform,payload,status,created_at,message FROM platform_receipts WHERE status IN ('submitting','submitted','unknown') ORDER BY created_at DESC LIMIT 50").fetchall()
        return [dict(id=r[0],platform=r[1],title=json.loads(r[2]).get('title',''),status=r[3],created_at=r[4],message=r[5]) for r in rows]

    def read_result(self,platform,payload,created_at):
        binding=self.binding(platform)
        page=self.page(platform)
        path=page.snapshot()['url']
        manager=('https://creator.douyin.com/creator-micro/content/manage' if platform=='douyin' else 'https://channels.weixin.qq.com/platform/post/'+('finderNewLifePostList' if payload['media_type']=='image' else 'list'))
        # The Channels home also contains native work cards. Prefer the current
        # authenticated page; opening a new tab can require a fresh QR login.
        rows=page.dom('management')
        compact=lambda value:re.sub(r'\s+','',str(value or ''))
        title=compact(payload.get('title'))
        content=compact(payload.get('content'))
        def matches_work(row):
            actual=compact(row.get('title'))
            # Douyin image titles are displayed as the prefix of its combined
            # description. Require the native work ID and a title delimiter.
            combined=bool(row.get('id') and title and actual.startswith(title+'。'))
            return ((title and title==actual) or combined or (content and content in compact(row['text']))) and (not row.get('created_at') or row['created_at']>=created_at-120)
        if path!=manager and not any(matches_work(row) for row in rows):
            opened=rpc(binding['client_id'],platform,'open',url=manager)
            page=Page({**binding,'tab_id':opened['id']})
            rows=[]
        elif platform=='douyin' and path==manager and not any(matches_work(row) for row in rows):
            page.call('evaluate',expression="(() => {setTimeout(()=>location.reload(),100);return true;})()")
            rows=[]
        for _ in range(12):
            if any(matches_work(row) for row in rows):break
            time.sleep(.5)
            try:rows=page.dom('management')
            except RuntimeError as exc:
                if not any(x in str(exc).lower() for x in ('frame with id','cannot access','showing error page')):raise
        snapshot=page.snapshot()
        if snapshot.get('account_id')!=binding.get('account_id'):raise ValueError('作品管理账号与任务不一致，未确认结果')
        matches=[row for row in rows if matches_work(row)]
        if len(matches)!=1:return {'status':'unknown','message':'官方列表没有找到唯一对应作品，请人工核对；不会重新发布','candidates':len(matches)}
        row=matches[0];text=row['text']
        status_text=row.get('status_text',text)
        if platform=='channels' and row.get('native_status')==1 and row.get('id'):state='published';message='已在官方作品管理找到对应作品及作品 ID'
        elif re.search('未通过|审核失败|发布失败|已删除',status_text):state='unknown';message='官方作品存在异常，请人工处理'
        elif re.search('将于|定时|待发表',status_text):state='scheduled';message='已在官方列表找到定时作品：'+text[:500]
        elif re.search('审核中|处理中',status_text):state='submitted';message='官方作品正在审核或处理'
        elif re.search('已发布|发布成功',status_text):state='published';message='官方列表明确显示已发布'
        else:state='submitted';message='已在官方列表找到对应作品，最终审核状态仍需确认'
        return {'status':state,'message':message,'work_id':row.get('id',''),'url':row.get('url',''),'official_text':text[:1500],'checked_at':time.time()}

    def handle(self, action, request):
        with self.app.BROWSER_LOCK:
            if action in ('xhs-prepare','xhs-submit'):
                import platform_xhs
                return (platform_xhs.prepare if action=='xhs-prepare' else platform_xhs.submit)(self.app,request)
            if action=='tabs':
                available=tabs();self.init()
                with self.app.db_connection() as db:rows=db.execute('SELECT platform,binding FROM platform_bindings').fetchall()
                bindings={p:json.loads(raw) for p,raw in rows}
                bindings={p:b for p,b in bindings.items() if any(t['platform']==p and t['id']==b['tab_id'] and t['client_id']==b['client_id'] for t in available)}
                return {'tabs':available,'bindings':bindings}
            if action=='bind':return self.bind(request)
            if action=='open':return self.open(request)
            if action=='upload':return self.upload(request)
            if action=='fill':return self.prepare(request)
            if action=='prepare':return self.prepare(request)
            if action=='verify':return self.verify(request)
            if action=='submit':return self.submit(request)
            if action=='options':return self.options(request)
            if action=='snapshot':return {'snapshot':public_snapshot(self.page(request['platform']).snapshot())}
            if action=='focus':self.page(request['platform']).call('focus');return {}
            if action=='history':
                import platform_xhs
                return {'history':sorted(self.history()+platform_xhs.history(self.app),key=lambda r:r['created_at'],reverse=True)[:50]}
            if action=='choose-files':return {'files':choose_files(request.get('media_type','image'))}
        raise ValueError('未知平台操作')


def file_stats(files):
    result=[]
    for f in files:
        path=Path(f).resolve();stat=path.stat();h=hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
        result.append([str(path),stat.st_size,stat.st_mtime_ns,h.hexdigest()])
    return result


def pending_upload(manifest):
    # Earlier interrupted uploads may have recorded the native placeholder.
    images=manifest.get('media',{}).get('images',[])
    return manifest.get('pending',False) or any(not x or x=='none' or x.startswith('data:') for x in images)


def public_snapshot(s):
    # Signed media URLs and internal page text are never returned to the app UI.
    return {k:v for k,v in s.items() if k not in ('media','video','summary')}


def choose_files(kind):
    types='{"public.movie"}' if kind=='video' else '{"public.image"}'
    script='set chosen to choose file with prompt "选择发布素材" of type '+types+' with multiple selections allowed\nset paths to {}\nrepeat with f in chosen\nset end of paths to POSIX path of f\nend repeat\nset AppleScript\'s text item delimiters to linefeed\nreturn paths as text'
    r=subprocess.run(['osascript','-e',script],capture_output=True,text=True,timeout=180)
    if r.returncode:
        if '-128' in r.stderr:return []
        raise ValueError('选择素材失败：'+r.stderr.strip())
    return r.stdout.strip().splitlines()
