'use strict';
const $ = id => document.getElementById(id);
document.querySelectorAll('[data-route]').forEach(button => button.addEventListener('click', () => window.desktop.navigate(button.dataset.route)));
document.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', () => window.desktop.action(button.dataset.action)));
window.desktop.onRoute(key => {
  document.querySelectorAll('[data-route]').forEach(button => {
    if (button.dataset.route === key) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
});
window.desktop.onMode(value => {
  document.documentElement.classList.toggle('compact-window',value?.compact === true);
  document.querySelector('.titlebar > span').textContent=value?.compact === true?'来袭预警 · 置顶':'率土战场';
});
window.desktop.onStatus(status => {
  $('statusText').textContent = status.text;
  $('statusDot').dataset.tone = status.tone;
  if (status.identity) { $('roleName').textContent = status.identity; $('role').hidden = false; }
  if ('union'in status) $('roleUnion').textContent = status.union;
  if (status.statisticsTitle) {
    document.querySelector('[data-route="attendance"] span').textContent=status.statisticsTitle;
    document.querySelector('.role-avatar').textContent=status.statisticsTitle==='流浪军战绩'?'浪':status.statisticsTitle==='个人战绩'?'人':'盟';
  }
});
window.desktop.onLoading(value => {
  $('loading').hidden = !value.show;
  $('loadingTitle').textContent = value.error ? '暂时无法连接' : '正在读取';
  $('loadingDetail').textContent = value.text || '';
  $('loader').hidden = !!value.error;
  $('errorActions').hidden = !value.error;
});
document.addEventListener('keydown', event => {
  if(event.defaultPrevented||event.isComposing)return;
  if(event.key==='Escape'&&!event.ctrlKey&&!event.altKey&&!event.metaKey&&!event.shiftKey){
    event.preventDefault();window.desktop.action('clear-focus');return;
  }
  if (event.ctrlKey && ['1','2','3','4'].includes(event.key)) {
    event.preventDefault(); window.desktop.navigate(['map','intel','archive','attendance'][Number(event.key)-1]);
  }
});
