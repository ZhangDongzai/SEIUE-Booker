// 倒计时与轻量交互。不依赖任何 CDN，离线可用。
(function () {
  function pad(n) { return n < 10 ? '0' + n : '' + n; }

  function format(ms) {
    if (ms <= 0) return '已到点';
    var total = Math.floor(ms / 1000);
    var d = Math.floor(total / 86400);
    var h = Math.floor((total % 86400) / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total % 60;
    if (d > 0) return d + ' 天 ' + pad(h) + ':' + pad(m) + ':' + pad(s);
    if (h > 0) return pad(h) + ':' + pad(m) + ':' + pad(s);
    return pad(m) + ':' + pad(s);
  }

  function tick() {
    var nodes = document.querySelectorAll('[data-countdown]');
    var now = Date.now();
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      var target = Number(el.getAttribute('data-countdown'));
      if (!target) continue;
      var diff = target - now;
      el.textContent = diff > 0 ? '还有 ' + format(diff) : format(0);
      if (diff > 0 && diff < 10 * 60 * 1000) {
        el.classList.add('soon');
      } else {
        el.classList.remove('soon');
      }
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    if (document.querySelector('[data-countdown]')) {
      tick();
      setInterval(tick, 500);
    }

    // 点击占位符即可复制
    document.querySelectorAll('[data-copy]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var text = btn.getAttribute('data-copy');
        if (navigator.clipboard) {
          navigator.clipboard.writeText(text);
          var old = btn.textContent;
          btn.textContent = '已复制';
          setTimeout(function () { btn.textContent = old; }, 900);
        }
      });
    });
  });
})();
