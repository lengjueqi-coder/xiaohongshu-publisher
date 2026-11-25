"""Reuse the established XHS image publisher in a reviewed distribution batch."""
import json
import time
import uuid

from platform_publishing import digest, file_stats


def submission_key(payload, binding):
    # A new batch freezes the same media in a different directory. Use the
    # reviewed bytes and official form values, independent of staging paths.
    fields = {key: payload.get(key) for key in (
        'media_type', 'title', 'content', 'tags', 'schedule_time', 'visibility',
        'original', 'allow_copy', 'activities', 'declaration', 'mentions', 'album', 'location',
        'shooting_date', 'shooting_location', 'repost_source',
    )}
    return digest({'payload': fields, 'account_id': binding['user_id'],
                   'files': [item[3] for item in binding['files']]})


def init(app):
    with app.db_connection() as db:
        db.execute('CREATE TABLE IF NOT EXISTS platform_xhs_receipts (id TEXT PRIMARY KEY, payload TEXT NOT NULL, prepared TEXT NOT NULL, binding TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL, message TEXT NOT NULL DEFAULT \'\')')
        if 'attempted_at' not in {r[1] for r in db.execute('PRAGMA table_info(platform_xhs_receipts)')}:
            db.execute('ALTER TABLE platform_xhs_receipts ADD COLUMN attempted_at REAL')
            for token, payload, binding in db.execute('SELECT id,payload,binding FROM platform_xhs_receipts').fetchall():
                key = submission_key(json.loads(payload), json.loads(binding))
                db.execute('UPDATE platform_xhs_receipts SET fingerprint=? WHERE id=?', (key,token))


def prepare(app, request):
    if request.get('media_type') not in ('image','video'):raise ValueError('小红书素材类型无效')
    payload=dict(request.get('payload') or {})
    profile=app.require_active_profile(payload)
    identity=app.account_identity(profile)
    if not identity.get('user_id'):raise ValueError('请先在小红书模块识别当前账号')
    if len(payload.get('tags',[]))>10:raise ValueError('小红书最多 10 个话题')
    files=[payload.get('video_path')] if request['media_type']=='video' else payload.get('images',[])
    if not files or any(not isinstance(f,str) or not f for f in files):raise ValueError('请选择小红书视频或图片素材')
    payload['media_type']=request['media_type']
    binding={'profile_dir':profile,'user_id':identity['user_id'],'files':file_stats(files)}
    key=submission_key(payload,binding)
    init(app)
    with app.db_connection() as db:
        if db.execute("SELECT 1 FROM platform_xhs_receipts WHERE fingerprint=? AND status IN ('submitting','submitted','unknown')",(key,)).fetchone():
            raise ValueError('小红书这份内容已经尝试发布，请先在笔记管理核对，避免重复')
        existing=db.execute("SELECT id,prepared,created_at FROM platform_xhs_receipts WHERE fingerprint=? AND status='verified' ORDER BY created_at DESC LIMIT 1",(key,)).fetchone()
    if existing:
        prepared=json.loads(existing[1]);pending=app.manual_pending(profile) or {}
        if pending.get('history_id')==prepared['history_id']:
            if request['media_type']=='image' and not (prepared.get('verification_payload') or {}).get('native_image_refs'):
                # Legacy receipts have no way to prove the displayed order.
                # Retire only this unsubmitted receipt before refilling.
                with app.db_connection() as db:
                    db.execute("UPDATE platform_xhs_receipts SET status='cancelled' WHERE id=? AND status='verified'", (existing[0],))
                app.clear_manual_pending(prepared['history_id'],profile)
            else:
                app.select_profile_connection(profile)
                app.check_task_identity({**payload,'expected_user_id':identity['user_id']})
                app.verify_prepared_publish(prepared,payload)
                with app.db_connection() as db:db.execute('UPDATE platform_xhs_receipts SET created_at=? WHERE id=?',(time.time(),existing[0]))
                return {'verified':True,'receipt':existing[0],'history_id':prepared['history_id'],'message':'已恢复并重新核对小红书待提交草稿'}
    app.select_profile_connection(profile)
    payload={**payload,'profile_dir':profile,'expected_user_id':identity['user_id']}
    app.check_task_identity(payload)
    if request['media_type']=='video':
        import platform_xhs_video
        prepared=platform_xhs_video.prepare(app,payload)
    else:prepared=app.prepare_publish(payload)
    app.db_set_setting('manual_pending:'+profile,{'history_id':prepared['history_id'],'title':payload.get('title'),'created_at':app.now_iso()})
    # The existing checker applies native mentions once, then verifies all settings.
    try:
        app.verify_prepared_publish(prepared,payload)
    except Exception:
        app.clear_manual_pending(prepared['history_id'],profile)
        raise
    token=uuid.uuid4().hex
    with app.db_connection() as db:
        db.execute('INSERT INTO platform_xhs_receipts (id,payload,prepared,binding,fingerprint,status,created_at) VALUES (?,?,?,?,?,?,?)',
                   (token,json.dumps(payload,ensure_ascii=False),json.dumps(prepared,ensure_ascii=False),json.dumps(binding),key,'verified',time.time()))
    return {'verified':True,'receipt':token,'history_id':prepared['history_id'],'message':'小红书素材已填写并核对，尚未提交'}


def submit(app, request):
    init(app);token=request.get('receipt','')
    with app.db_connection() as db:
        row=db.execute('SELECT payload,prepared,binding,status,created_at FROM platform_xhs_receipts WHERE id=?',(token,)).fetchone()
    if not row or row[3]!='verified':raise ValueError('小红书校验已失效或已提交，不会自动重试')
    if time.time()-row[4]>1800:raise ValueError('小红书校验超过 30 分钟，请重新核对')
    payload,prepared,binding=[json.loads(v) for v in row[:3]]
    app.require_active_profile(payload)
    files=[payload.get('video_path')] if payload.get('media_type')=='video' else payload.get('images',[])
    if file_stats(files)!=binding['files']:raise ValueError('小红书素材已变化，已阻止提交')
    pending=app.manual_pending(binding['profile_dir']) or {}
    if pending.get('history_id')!=prepared['history_id']:raise ValueError('小红书待确认页面已变化，请重新核对')
    app.select_profile_connection(binding['profile_dir'])
    app.check_task_identity(payload)
    from publish_checks import verify
    effective=prepared.get('verification_payload') or payload
    if payload.get('media_type')=='image' and not effective.get('native_image_refs'):
        raise ValueError('这份小红书图文缺少图片核对凭证，请重新填写')
    verify(app.XHS_SKILL_DIR,effective,apply_mentions=False)
    app.require_schedule_lead(effective)
    with app.db_connection() as db:
        changed=db.execute("""UPDATE platform_xhs_receipts SET status='submitting',attempted_at=?
            WHERE id=? AND status='verified' AND NOT EXISTS (
              SELECT 1 FROM platform_xhs_receipts WHERE fingerprint=?
              AND status IN ('submitting','submitted','unknown'))""",(time.time(),token,submission_key(payload,binding))).rowcount
    if changed!=1:raise ValueError('小红书内容已提交过，请查看笔记管理')
    try:
        result=app.click_publish(prepared['history_id'])
        if result.get('ok'):
            app.clear_manual_pending(prepared['history_id'],binding['profile_dir'])
            status='submitted';message='小红书已收到平台提交确认，最终审核状态请到笔记管理查看'
        else:
            status='unknown';message=result.get('message') or '小红书已尝试提交，结果待核对，请勿重复发布'
    except Exception as exc:
        status='unknown';message='小红书提交结果待核对：'+str(exc)
    with app.db_connection() as db:db.execute('UPDATE platform_xhs_receipts SET status=?,message=? WHERE id=?',(status,message,token))
    return {'status':status,'message':message,'receipt':token}


def history(app):
    init(app)
    with app.db_connection() as db:
        rows=db.execute("SELECT id,payload,status,created_at,message FROM platform_xhs_receipts WHERE status IN ('submitting','submitted','unknown') ORDER BY created_at DESC LIMIT 50").fetchall()
    return [dict(id=r[0],platform='xhs',title=json.loads(r[1]).get('title',''),status=r[2],created_at=r[3],message=r[4]) for r in rows]
