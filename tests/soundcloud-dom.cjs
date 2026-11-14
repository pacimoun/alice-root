const assert = require('node:assert/strict');
const {parseHTML} = require('linkedom');
const {execFileSync} = require('node:child_process');
const path = require('node:path');
const pageSource = execFileSync(process.env.PYTHON || 'python3', ['-c',
  'import ast,pathlib; m=ast.parse(pathlib.Path("src/alice_root/soundcloud.py").read_text()); print(next(ast.literal_eval(n.value) for n in m.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=="PAGE" for t in n.targets)))'],
  {cwd: path.resolve(__dirname, '..'), encoding:'utf8'});
const page = eval(pageSource);
const origin = 'https://soundcloud.com';
let clicks, hovers;
function setup(html, path='/you/likes') {
  const {window} = parseHTML('<html><head><title>Likes</title></head><body>'+html+'</body></html>');
  global.document=window.document;
  global.location={origin,pathname:path};
  Object.defineProperty(global,'navigator',{value:{mediaSession:{}},configurable:true});
  Object.defineProperty(document,'readyState',{value:'complete',configurable:true});
  window.HTMLElement.prototype.getClientRects=function(){return this.closest('[hidden]')?[]:[{}];};
  global.getComputedStyle=el=>({visibility:el.closest('[data-hover-hidden]')?'hidden':'visible'});
  global.MouseEvent=window.Event;
  clicks=[]; hovers=[];
  document.addEventListener('click',e=>clicks.push(e.target.id||e.target.className));
  document.addEventListener('mouseover',e=>hovers.push(e.target.className));
}
function tile(slug, options={}) {
  return `<div class="audibleTile playableTile" ${options.attrs||''}>
    <a class="playableTile__artworkLink audibleTile__artworkLink" href="${origin}/artist/${slug}"></a>
    <div class="playableTile__playButton audibleTile__playButton" data-hover-hidden>
      <button id="${slug}" class="playButton sc-button-play ${options.classes||''}" ${options.buttonAttrs||''} title="Play"></button>
    </div>
    <a class="playableTile__heading audibleTile__audibleHeading" href="${origin}/artist/${slug}">Song ${slug}</a>
  </div>`;
}
let count=0;
function test(name,fn) {fn(); count++; console.log('PASS '+name);}

test('legacy search list still plays a track',()=>{
  setup('<li class="soundList__item"><div class="sound"><a class="soundTitle__title" href="'+origin+'/artist/searched">Searched</a><button id="searched" class="playButton sc-button-play">Play</button></div></li>','/search/sounds');
  const s=page({op:'play-first'});
  assert.equal(s.selected_url,origin+'/artist/searched');
  assert.deepEqual(clicks,['searched']);
});
test('likes grid selects its hidden hover play button',()=>{
  setup(tile('liked'));
  const s=page({op:'play-first'});
  assert.equal(s.ok,true); assert.equal(s.selected_url,origin+'/artist/liked');
  assert.equal(s.selected_title,'Song liked'); assert.deepEqual(clicks,['liked']);
  assert.equal(hovers.length,1);
});
test('a title link is preferred over the artwork link',()=>{
  setup(tile('liked'));
  assert.equal(page({op:'play-first'}).selected_title,'Song liked');
});
test('playlist tiles are skipped in favor of a track',()=>{
  setup('<div class="playableTile"><a class="playableTile__heading" href="'+origin+'/artist/sets/album">Album</a><div class="playableTile__playButton"><button id="album" class="playButton">Play</button></div></div>'+tile('track'));
  assert.equal(page({op:'play-first'}).selected_url,origin+'/artist/track');
  assert.deepEqual(clicks,['track']);
});
test('disabled controls cannot be forced by hidden-button support',()=>{
  for(const disabled of [{classes:'sc-button-disabled'},{buttonAttrs:'aria-disabled="true"'},{buttonAttrs:'disabled'}]) {
    setup(tile('blocked',disabled)+tile('allowed'));
    const b=document.querySelector('#blocked');
    if(b.hasAttribute('disabled')) Object.defineProperty(b,'disabled',{value:true});
    assert.equal(page({op:'play-first'}).selected_url,origin+'/artist/allowed');
    assert.deepEqual(clicks,['allowed']);
  }
});
test('a tile configured never to show a play button is skipped',()=>{
  setup(tile('never',{attrs:'data-playButton="never"'})+tile('allowed'));
  assert.equal(page({op:'play-first'}).selected_url,origin+'/artist/allowed');
  assert.deepEqual(clicks,['allowed']);
});
test('hover-hidden legacy trackItem controls are supported',()=>{
  setup('<li class="trackItem"><a class="trackItem__trackTitle" href="'+origin+'/artist/row">Row</a><div class="trackItem__play" data-hover-hidden><button id="row" class="playButton">Play</button></div></li>');
  assert.equal(page({op:'play-first'}).selected_url,origin+'/artist/row');
  assert.deepEqual(clicks,['row']);
});
test('hidden whole cards are skipped',()=>{
  setup(tile('hidden',{attrs:'hidden'})+tile('visible'));
  assert.equal(page({op:'play-first'}).selected_url,origin+'/artist/visible');
  assert.deepEqual(clicks,['visible']);
});
test('sidebar and player cards cannot replace the likes list',()=>{
  setup('<aside>'+tile('aside')+'</aside><div class="l-sidebar">'+tile('sidebar')+'</div><div class="playControls">'+tile('player')+'<button id="old" class="playControl">Play current</button></div>');
  assert.equal(page({op:'play-first'}).code,'TRACK_LIST_NOT_READY');
  assert.deepEqual(clicks,[]);
});
test('like buttons and arbitrary hidden buttons are not clicked',()=>{
  setup('<div class="audibleTile"><a class="audibleTile__audibleHeading" href="'+origin+'/artist/track">Track</a><button id="like" class="sc-button-like">Like</button><div data-hover-hidden><button id="wrong" class="sc-button-play">Play</button></div></div>');
  assert.equal(page({op:'play-first'}).code,'TRACK_LIST_NOT_READY');
  assert.deepEqual(clicks,[]);
});
test('an already playing likes tile is not paused',()=>{
  setup(tile('current',{classes:'sc-button-pause'}));
  const s=page({op:'play-first'});
  assert.equal(s.clicked,false); assert.deepEqual(clicks,[]);
});
test('an unexpected origin or page cannot trigger card playback',()=>{
  setup(tile('other'),'/artist/profile');
  assert.equal(page({op:'play-first'}).code,'UNEXPECTED_PAGE');
  global.location.origin='https://other.example';
  assert.equal(page({op:'play-first'}).code,'PAGE_LOADING');
  assert.deepEqual(clicks,[]);
});
test('layout diagnostics detect hover controls without leaking queries',()=>{
  setup(tile('liked')+'<div class="new-card-layout"><a class="new-title" href="'+origin+'/artist/new?private=data">New</a></div>');
  const s=page({op:'diagnose'});
  assert.equal(s.tiles,1); assert.equal(s.list_items,0);
  assert.equal(s.track_cards[0].play_button.visible,false);
  assert.equal(s.track_cards[0].play_button.enabled,true);
  assert.equal(s.track_links[1].url,origin+'/artist/new');
  assert(!JSON.stringify(s).includes('private=data'));
});
test('global pause and resume remain idempotent',()=>{
  setup('<div class="playControls"><button id="global" class="playControl playing">Pause current</button></div>');
  assert.equal(page({op:'resume'}).clicked,false); assert.deepEqual(clicks,[]);
  assert.equal(page({op:'pause'}).clicked,true); assert.deepEqual(clicks,['global']);
});
test('track hero playback remains available',()=>{
  setup('<div class="fullListenHero"><button id="hero" class="playButton">Play</button></div>','/artist/track');
  assert.equal(page({op:'play-track'}).selected_url,origin+'/artist/track');
  assert.deepEqual(clicks,['hero']);
});
console.log('DOM_TESTS_OK='+count);
