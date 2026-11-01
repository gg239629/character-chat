/* Fit settings to the available window height. Page through fields, never hide them. */
(()=>{
  function paginate(heights,available){
    const pages=[[]];let used=0;
    heights.forEach((height,index)=>{
      if(height<=0)return;
      if(used && used+height>available){pages.push([]);used=0;}
      pages[pages.length-1].push(index);used+=height;
    });
    return pages;
  }
  window.settingsPagination=paginate;
  window.mountSettingsLayout=function(dialog){
    if(dialog.dataset.paged)return;
    dialog.dataset.paged='true';dialog.classList.add('paged-settings');
    const panels=[...dialog.querySelectorAll(':scope > [role="tabpanel"]')];
    if(!panels.length){
      const panel=document.createElement('section');panel.className='settings-panel';
      [...dialog.children].filter(e=>!e.matches('h2,.actions')).forEach(e=>panel.append(e));
      dialog.insertBefore(panel,dialog.querySelector(':scope > .actions'));
      panels.push(panel);
    }
    const layouts=[];
    for(const panel of panels){
      panel.classList.add('settings-panel');
      const viewport=document.createElement('div');viewport.className='settings-viewport';
      const items=[];
      for(const child of [...panel.children]){
        if(child.matches('.dlgFoot,.profile-actions,[role="status"],.status'))continue;
        const wrapper=document.createElement('div');wrapper.className='settings-item';wrapper.append(child);viewport.append(wrapper);items.push(wrapper);
      }
      panel.prepend(viewport);
      const nav=document.createElement('nav');nav.className='settings-pagination';nav.setAttribute('aria-label','设置分页');
      const previous=document.createElement('button'),next=document.createElement('button'),label=document.createElement('span');
      previous.type=next.type='button';previous.textContent='上一页';next.textContent='下一页';
      previous.className=next.className='ghost quiet';label.setAttribute('aria-live','polite');
      nav.append(previous,label,next);viewport.after(nav);
      let page=0,pages=[[]];
      function show(){
        items.forEach((item,index)=>item.hidden=!pages[page].includes(index));
        previous.disabled=page===0;next.disabled=page===pages.length-1;
        label.textContent=`${page+1} / ${pages.length}`;
        nav.hidden=pages.length===1;
      }
      function layout(){
        if(!dialog.open || panel.hidden)return;
        // Reserve navigation space while measuring, so adding it cannot overflow.
        nav.hidden=false;items.forEach(item=>item.hidden=false);
        const heights=items.map(item=>item.getBoundingClientRect().height);
        pages=paginate(heights,viewport.clientHeight);
        page=Math.min(page,pages.length-1);show();
      }
      previous.onclick=()=>{page--;show();if(previous.disabled)next.focus();};
      next.onclick=()=>{page++;show();if(next.disabled)previous.focus();};
      layouts.push(layout);
      new ResizeObserver(schedule).observe(viewport);
      new MutationObserver(changes=>{
        if(changes.some(c=>c.type!=='attributes'||!c.target.classList.contains('settings-item')))schedule();
      }).observe(viewport,{childList:true,subtree:true,characterData:true,attributes:true,attributeFilter:['hidden']});
    }
    let pending=false;
    function schedule(){if(pending)return;pending=true;requestAnimationFrame(()=>{pending=false;layouts.forEach(layout=>layout());});}
    new ResizeObserver(schedule).observe(dialog);
    new MutationObserver(schedule).observe(dialog,{attributes:true,subtree:true,attributeFilter:['open','aria-selected']});
    window.addEventListener('resize',schedule);schedule();
  };
  ['settingsDlg','homeSettingsDialog'].forEach(id=>{const dialog=document.getElementById(id);if(dialog)window.mountSettingsLayout(dialog);});
})();
