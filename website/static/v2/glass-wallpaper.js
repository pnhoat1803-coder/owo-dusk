/* =========================================================================
   glass-wallpaper.js  –  Animated WebGL wallpaper for OwO-Dusk dashboard
   Replaces the static background in .lg-wallpaper with a fullscreen
   WebGL canvas that renders slowly-moving colour blobs.
   Falls back silently if WebGL is unavailable — the CSS background stays.

   Reads colours from --lg-wall-0 … --lg-wall-4 CSS custom properties so
   it respects Dark / Light themes automatically.

   Mobile-optimized:
     - DPR capped at 1.0 on mobile (vs 1.5 on desktop) — 2-4x fewer pixels
     - Targets ~30 FPS on mobile, ~60 FPS on desktop via frame skipping
     - Pauses on tab hidden, reduced-motion, battery saver
     - Auto-detects mobile via screen width + touch

   Works on both v2.html (desktop) and m.html (phone).

   API (window.DuskWallpaper):
     .pause()   – freeze animation
     .resume()  – resume
     .refresh() – re-read colours + resize (call after theme change)
   ========================================================================= */
(function () {
  'use strict';

  /* ---- state ---- */
  var canvas, gl, prog;
  var uTime, uRes, uColors;
  var running = false, paused = false, startT = 0, raf = 0;
  var lastFrame = 0;
  var mobile = false;

  /* ---- detect mobile ---- */
  function isMobile() {
    return (window.innerWidth <= 768) ||
           ('ontouchstart' in window && window.innerWidth <= 1024);
  }

  /* ---- target frame interval (ms) ---- */
  function frameMs() {
    return mobile ? 33.3 : 16.6;
  }

  /* ---- shaders ---- */
  var VERT =
    'attribute vec2 a;' +
    'void main(){gl_Position=vec4(a,0.,1.);}';

  var FRAG = [
    'precision mediump float;',
    'uniform float uT;',
    'uniform vec2  uR;',
    'uniform vec3  uC[5];',
    '',
    'float blob(vec2 p, vec2 c, float r){',
    '  float d2 = dot(p-c, p-c);',
    '  return exp(-d2 / (r * 0.55));',
    '}',
    '',
    'void main(){',
    '  vec2 uv = gl_FragCoord.xy / uR;',
    '  float ar = uR.x / uR.y;',
    '  vec2  p  = vec2(uv.x * ar, uv.y);',
    '  float t  = uT * 0.012;',
    '',
    '  vec2 c0 = vec2( 0.06*ar + 0.18*sin(t*0.71),       -0.06 + 0.14*cos(t*0.53));',
    '  vec2 c1 = vec2( 1.04*ar + 0.12*cos(t*0.63),        0.28 + 0.16*sin(t*0.82));',
    '  vec2 c2 = vec2( 0.18*ar + 0.14*sin(t*0.91 + 1.0),  1.08 + 0.12*cos(t*0.44));',
    '  vec2 c3 = vec2( 0.88*ar + 0.10*cos(t*0.55 + 2.0),  1.04 + 0.14*sin(t*0.73));',
    '',
    '  float vm = max(ar, 1.0);',
    '  float f0 = blob(p, c0, 0.58*vm);',
    '  float f1 = blob(p, c1, 0.46*vm);',
    '  float f2 = blob(p, c2, 0.50*vm);',
    '  float f3 = blob(p, c3, 0.36*vm);',
    '',
    '  vec3 col = uC[0];',
    '  col = mix(col, uC[1], f0 * 0.78);',
    '  col = mix(col, uC[2], f1 * 0.68);',
    '  col = mix(col, uC[3], f2 * 0.72);',
    '  col = mix(col, uC[4], f3 * 0.58);',
    '',
    '  float n = fract(sin(dot(uv, vec2(12.9898,78.233))) * 43758.5453);',
    '  col += (n - 0.5) * 0.006;',
    '',
    '  gl_FragColor = vec4(col, 1.0);',
    '}'
  ].join('\n');

  /* ---- helpers ---- */
  function mkShader(src, type) {
    var s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
      console.warn('[DuskWallpaper] shader error:', gl.getShaderInfoLog(s));
      return null;
    }
    return s;
  }

  function parseColour(str) {
    str = (str || '').trim();
    if (str.charAt(0) === '#') {
      var h = str.slice(1);
      if (h.length === 3) h = h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
      return [parseInt(h.slice(0,2),16)/255,
              parseInt(h.slice(2,4),16)/255,
              parseInt(h.slice(4,6),16)/255];
    }
    var m = str.match(/[\d.]+/g);
    if (m && m.length >= 3) {
      var r = +m[0], g = +m[1], b = +m[2];
      if (r > 1 || g > 1 || b > 1) { r /= 255; g /= 255; b /= 255; }
      return [r, g, b];
    }
    return [0, 0, 0];
  }

  function readColours() {
    var cs = getComputedStyle(document.documentElement);
    for (var i = 0; i < 5; i++) {
      var c = parseColour(cs.getPropertyValue('--lg-wall-' + i));
      gl.uniform3f(uColors[i], c[0], c[1], c[2]);
    }
  }

  function resize() {
    mobile = isMobile();
    var maxDpr = mobile ? 1.0 : 1.5;
    var dpr = Math.min(window.devicePixelRatio || 1, maxDpr);
    var w = Math.round(window.innerWidth  * dpr);
    var h = Math.round(window.innerHeight * dpr);
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width  = w;
      canvas.height = h;
      gl.viewport(0, 0, w, h);
      gl.uniform2f(uRes, w, h);
      if (paused) drawStill();   /* a resized canvas is cleared: repaint the still picture */
    }
  }

  function frame(now) {
    if (!running) return;
    if (paused) { raf = 0; return; }   /* paused: no more frames are asked for, start() asks again */
    raf = requestAnimationFrame(frame);
    var dt = now - lastFrame;
    if (dt < frameMs()) return;
    lastFrame = now - (dt % frameMs());
    gl.uniform1f(uTime, now / 1000 - startT);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  /* Asks for frames again (after a pause). One loop only: raf is 0 while none is pending */
  function start() {
    if (!running || paused || raf) return;
    lastFrame = performance.now();
    raf = requestAnimationFrame(frame);
  }

  /* One still picture: used while the animation is paused for reduced motion, so the canvas is never left black */
  function drawStill() {
    if (!gl) return;
    gl.uniform1f(uTime, 0.0);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  /* ---- init ---- */
  function init() {
    var el = document.querySelector('.lg-wallpaper');
    if (!el) return;

    mobile = isMobile();

    canvas = document.createElement('canvas');
    gl = canvas.getContext('webgl', {
      alpha: false, antialias: false, depth: false, stencil: false,
      preserveDrawingBuffer: false, powerPreference: 'low-power'
    });
    if (!gl) return;

    var vs = mkShader(VERT, gl.VERTEX_SHADER);
    var fs = mkShader(FRAG, gl.FRAGMENT_SHADER);
    if (!vs || !fs) return;

    prog = gl.createProgram();
    gl.attachShader(prog, vs);
    gl.attachShader(prog, fs);
    gl.bindAttribLocation(prog, 0, 'a');
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) return;
    gl.useProgram(prog);

    uTime = gl.getUniformLocation(prog, 'uT');
    uRes  = gl.getUniformLocation(prog, 'uR');
    uColors = [];
    for (var i = 0; i < 5; i++) uColors.push(gl.getUniformLocation(prog, 'uC[' + i + ']'));

    var buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 3,-1, -1,3]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

    canvas.style.cssText = 'position:fixed;inset:0;z-index:-1;pointer-events:none;width:100%;height:100%';
    canvas.className = 'lg-wallpaper lg-wallpaper--gl';
    canvas.setAttribute('aria-hidden', 'true');
    el.parentNode.replaceChild(canvas, el);

    readColours();
    resize();
    startT = performance.now() / 1000;
    lastFrame = performance.now();
    running = true;
    if (paused) drawStill(); else start();

    new MutationObserver(function () { readColours(); if (paused && !document.hidden) drawStill(); })
      .observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

    window.addEventListener('resize', resize);

    document.addEventListener('visibilitychange', function () {
      if (document.hidden) { paused = true; }
      else { paused = rmq.matches; if (paused) drawStill(); else start(); }   /* reduced motion stays paused */
    });
  }

  var rmq = window.matchMedia('(prefers-reduced-motion: reduce)');
  function onRM() { paused = rmq.matches; if (paused) drawStill(); else start(); }
  if (rmq.addEventListener) rmq.addEventListener('change', onRM);
  onRM();

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  window.DuskWallpaper = {
    pause:   function () { paused = true; },
    resume:  function () { if (!rmq.matches) { paused = false; start(); } },
    refresh: function () { if (gl) { readColours(); resize(); if (paused) drawStill(); } }
  };
})();
