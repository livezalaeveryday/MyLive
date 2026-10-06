(() => {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const items = document.querySelectorAll('.reveal');
  if (!reducedMotion.matches && 'IntersectionObserver' in window) {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('is-visible');
        observer.unobserve(entry.target);
      });
    }, { threshold: 0.08 });
    items.forEach((item) => observer.observe(item));
  }
  let queued = false;
  const update = () => {
    const range = document.documentElement.scrollHeight - window.innerHeight;
    const progress = range > 0 ? Math.min(1, Math.max(0, window.scrollY / range)) : 0;
    document.documentElement.style.setProperty('--reading-progress', String(progress));
    queued = false;
  };
  const schedule = () => {
    if (!queued) { queued = true; requestAnimationFrame(update); }
  };
  addEventListener('scroll', schedule, { passive: true });
  addEventListener('resize', schedule);
  update();
})();
