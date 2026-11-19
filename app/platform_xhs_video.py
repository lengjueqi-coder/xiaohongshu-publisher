"""XHS video form adapter sharing verified receipts and the existing click boundary."""
import json
import tempfile
import time
from pathlib import Path


def prepare(app,payload):
    from publish_checks import page_for,verify
    profile=app.require_active_profile(payload)
    if app.manual_pending(profile):raise ValueError('小红书已有待确认页面，请先核实或放弃旧草稿')
    path=Path(payload.get('video_path','')).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() not in ('.mp4','.mov','.m4v','.webm'):raise ValueError('请选择有效的视频文件')
    if payload.get('schedule_time'):app.require_schedule_lead(payload)
    with tempfile.TemporaryDirectory(prefix='xhs-video-text-') as folder:
        title=Path(folder)/'title.txt';body=Path(folder)/'body.txt'
        title.write_text(payload['title'],encoding='utf-8');body.write_text(payload['content'],encoding='utf-8')
        args=['fill-publish-video','--title-file',str(title),'--content-file',str(body),'--video',str(path),'--visibility',payload.get('visibility','公开可见')]
        if payload.get('tags'):args+=['--tags',*payload['tags']]
        if payload.get('schedule_time'):args+=['--schedule-at',payload['schedule_time']]
        result=app.run_xhs_cli(args,timeout=720,extra_env=app.pace_environment())
    if not result.get('ok'):raise ValueError('小红书视频填写失败：'+str(result.get('stderr') or result.get('stdout') or '')[-1000:])
    page=page_for(app.XHS_SKILL_DIR)
    from xhs.publish import _set_original,_set_album_best_effort,_set_location_best_effort
    _set_original(page,bool(payload.get('original',True)))
    if payload.get('album'):_set_album_best_effort(page,payload['album'])
    if payload.get('location'):_set_location_best_effort(page,payload['location'])
    effective={**payload,'media_type':'video','images':[]}
    from publish_checks import apply_extra_settings
    apply_extra_settings(app.XHS_SKILL_DIR,effective)
    # Mentions are applied by the shared checker, once, after this adapter.
    state=page.evaluate(__import__('publish_checks').FORM_STATE)
    video=state.get('video') or {}
    if not video.get('src') or not video.get('duration'):raise ValueError('小红书视频预览尚未就绪')
    effective['native_video_src']=state['video']['src']
    history_id=app.record_history(effective,'prefilled',['小红书视频；原视频保留在固定任务目录'])
    return {'history_id':history_id,'verification_payload':effective,'payload':effective}
