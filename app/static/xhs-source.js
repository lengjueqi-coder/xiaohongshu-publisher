async function fillSource(data) {
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  const visible = e => e.getClientRects().length > 0;
  const pick = (selector, scope = document) => [...scope.querySelectorAll(selector)].filter(visible);
  const unique = (items, label) => {
    if (items.length !== 1) throw Error('无法唯一确认' + label);
    return items[0];
  };
  const dialog = unique(pick('.self-shooting-contain'), '来源声明弹窗');
  function type(input, value) {
    input.focus();
    input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }
  if (data.repost) {
    type(unique(pick('input[placeholder="请输入媒体名称"]', dialog), '转载媒体输入框'), data.source);
  } else {
    const location = unique(pick('input[placeholder="下拉选择地点"]', dialog), '拍摄地点');
    type(location, data.location);
    let places = [];
    for (let attempt = 0; attempt < 20; attempt++) {
      await delay(200);
      places = pick('.el-autocomplete-suggestion li,[role=option]').filter(e => e.innerText.split('\n')[0].trim() === data.location);
      if (places.length) break;
    }
    unique(places, '拍摄地点“' + data.location + '”').click();
    const picker = unique(pick('.d-datepicker-wrapper', dialog), '拍摄日历');
    picker.focus();
    const [year, month, day] = data.date.split('-').map(Number);
    for (let attempt = 0; attempt < 40; attempt++) {
      await delay(120);
      const calendar = unique(pick('.d-datepicker-calendar'), '拍摄日历');
      const [y, m] = pick('.d-datepicker-selector h6', calendar).map(e => parseInt(e.innerText, 10));
      if (year === y && month === m) {
        const dates = pick('.d-datepicker-cell.d-clickable:not(.--color-text-placeholder)', calendar);
        unique(dates.filter(e => Number(e.innerText.trim()) === day), '拍摄日期').click();
        break;
      }
      const nav = pick('.d-datepicker-header > div > .d-icon', calendar);
      if (nav.length !== 4) throw Error('无法确认日历翻页按钮');
      nav[year < y ? 0 : year > y ? 3 : month < m ? 1 : 2].click();
    }
    await delay(150);
    if (location.value !== data.location || dialog.querySelector('.d-datepicker-content')?.innerText.trim() !== data.date)
      throw Error('小红书没有保留指定的拍摄日期和地点');
  }
  await delay(180);
  const confirm = unique(pick('button', dialog).filter(e => e.innerText.trim() === '确认' && !e.disabled), '确认来源声明按钮');
  confirm.click();
  await delay(300);
  if (visible(dialog)) throw Error('小红书来源声明弹窗尚未确认');
  return true;
}
