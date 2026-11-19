const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, 'static/platform-dom.js'), 'utf8');

function management(platform, {title, status, post, body}) {
  const element = (value) => ({innerText:value, getClientRects:()=>[{}]});
  const card = {
    ...element(body), getAttribute:()=>null, querySelectorAll:()=>[],
    querySelector(selector) {
      if(selector.includes('info-status-')) return element(status);
      if(selector.includes('title')) return element(title);
      return null;
    },
  };
  if(platform==='douyin')card.__reactFiber$test={memoizedProps:{},return:{memoizedProps:{data:post}}};
  else card.__vue__={$props:{post}};
  const root = {querySelector:()=>null, querySelectorAll:()=>[card]};
  const fn = vm.runInNewContext('('+source+')', {document:root, getComputedStyle:()=>({visibility:'visible'}), URL});
  return fn({action:'management',platform});
}

test('current Douyin cards preserve native work ID, title and status separately from caption', async()=>{
  const [row]=await management('douyin',{
    title:'填写验证。正文说定时与审核失败',status:'已发布',body:'填写验证。正文说定时与审核失败\n已发布',
    post:{aweme_id:'123456',create_time:1000,share_url:'https://www.iesdouyin.com/share/note/123456/?tracking=private'},
  });
  assert.equal(row.id,'123456');assert.equal(row.created_at,1000);
  assert.equal(row.title,'填写验证。正文说定时与审核失败');assert.equal(row.status_text,'已发布');
  assert.equal(row.url,'https://www.iesdouyin.com/share/note/123456/');
});

test('Channels scheduled wording in the caption is excluded from status text', async()=>{
  const [row]=await management('channels',{
    title:'定时视频验证',body:'定时视频验证\n2026年11月19日 02:33\n分享',
    post:{objectId:'export/actual',createTime:1000,status:1},
  });
  assert.equal(row.id,'export/actual');assert.equal(row.native_status,1);
  assert.equal(row.status_text.includes('定时'),false);
});

test('Channels navigation preserves existing media instead of replacing a draft', async()=>{
  let navigated=false;
  const media={getClientRects:()=>[{}]};
  const root={querySelector:()=>null,querySelectorAll:selector=>selector==='.thumb-item,video'?[media]:[]};
  const fn=vm.runInNewContext('('+source+')',{document:root,getComputedStyle:()=>({visibility:'visible'})});
  await assert.rejects(fn({action:'navigate_empty_channels',platform:'channels',path:'/platform/post/create'}),/已有素材/);
  assert.equal(navigated,false);
});

test('Channels empty-form navigation uses the authenticated official router', async()=>{
  let target='';
  const app={$route:{path:'/platform'},$router:{async push(value){target=value;}}};
  const root={querySelector:selector=>selector==='#app'?{__vue__:app}:null,querySelectorAll:()=>[]};
  const fn=vm.runInNewContext('('+source+')',{document:root,getComputedStyle:()=>({visibility:'visible'})});
  await fn({action:'navigate_empty_channels',platform:'channels',path:'/platform/post/finderNewLifeCreate'});
  assert.equal(target,'/platform/post/finderNewLifeCreate');
});

function clickPoint(rect, covered=false){
  const target={innerText:'发布',className:'',getClientRects:()=>[{}],getAttribute:()=>null,scrollIntoView(){},getBoundingClientRect:()=>rect,contains:hit=>hit===target};
  const root={querySelector:()=>null,querySelectorAll:selector=>selector==='button'?[target]:[],elementFromPoint:()=>covered?{}:target};
  const fn=vm.runInNewContext('('+source+')',{document:root,getComputedStyle:()=>({visibility:'visible'}),innerWidth:500,innerHeight:712});
  return fn({action:'point',platform:'douyin',button:'发布'});
}

test('offscreen publish button never returns a negative click coordinate', async()=>{
  await assert.rejects(clickPoint({left:-194,right:-74,top:600,bottom:632}),/可见区域外/);
});

test('covered publish button stops instead of clicking the overlay', async()=>{
  await assert.rejects(clickPoint({left:20,right:140,top:600,bottom:632},true),/遮挡/);
});

test('partially visible controls use the visible portion only', async()=>{
  const point=await clickPoint({left:-100,right:100,top:600,bottom:632});
  assert.equal(point.x,50);assert.equal(point.y,616);
});

test('Channels selects a track with the row action without triggering preview', async () => {
  let selected = false;
  let hovered = false;
  const row = {
    innerText: '测试曲', getClientRects: () => [{}], matches: s => s === '.bgm-item-wrap',
    dispatchEvent: event => { hovered = event.type === 'mouseenter'; },
    querySelectorAll: s => s === 'button' && hovered ? [{innerText:'选择',click:()=>{ selected = true; }}] : [],
    click() { throw Error('点到了试听'); }
  };
  const root = {querySelector:()=>null, querySelectorAll:()=>[row]};
  class MouseEvent { constructor(type) { this.type = type; } }
  const fn = vm.runInNewContext('('+source+')', {document:root, MouseEvent, getComputedStyle:()=>({visibility:'visible'})});
  await fn({action:'activate', platform:'channels', field:'music', option:'测试曲'});
  assert.equal(selected, true);
});
