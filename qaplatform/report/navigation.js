(() => {
  const nav = document.querySelector('.jump-nav');
  const sections = Array.from(nav.querySelectorAll('a[href^="#"]'), link => ({
    link,
    section: document.getElementById(link.hash.slice(1)),
  })).filter(item => item.section);
  if (!sections.length) return;

  let current = sections.find(item => item.link.hasAttribute('aria-current'));
  let anchorScrollY;
  let scheduled = false;

  function select(item) {
    if (current === item) return;
    current?.link.removeAttribute('aria-current');
    current = item;
    item.link.setAttribute('aria-current', 'location');

    const left = item.link.offsetLeft;
    const right = left + item.link.offsetWidth;
    if (left < nav.scrollLeft) {
      nav.scrollLeft = left;
    } else if (right > nav.scrollLeft + nav.clientWidth) {
      nav.scrollLeft = right - nav.clientWidth;
    }
  }

  function selectFromScroll() {
    if (anchorScrollY === window.scrollY) return;
    anchorScrollY = undefined;
    const atBottom = window.scrollY > 0 &&
      window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 1;
    if (atBottom) {
      select(sections[sections.length - 1]);
      return;
    }

    const readingLine = nav.offsetHeight + 24;
    let visible = sections[0];
    for (const item of sections) {
      if (item.section.getBoundingClientRect().top > readingLine) break;
      visible = item;
    }
    select(visible);
  }

  function selectFromHash() {
    const target = document.getElementById(window.location.hash.slice(1));
    const item = target && sections.find(item => item.section.contains(target));
    if (item) {
      // Short sections can share the same landing position at the end of the page.
      anchorScrollY = window.scrollY;
      select(item);
    } else {
      anchorScrollY = undefined;
      selectFromScroll();
    }
  }

  nav.addEventListener('click', event => {
    if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey &&
        event.target.closest('a[href^="#"]')) {
      // A second click on the same anchor scrolls without firing hashchange.
      window.requestAnimationFrame(selectFromHash);
    }
  });
  window.addEventListener('hashchange', selectFromHash);
  window.addEventListener('load', selectFromHash);
  window.addEventListener('scroll', () => {
    if (scheduled) return;
    scheduled = true;
    window.requestAnimationFrame(() => {
      scheduled = false;
      selectFromScroll();
    });
  }, {passive: true});
  selectFromHash();
})();
