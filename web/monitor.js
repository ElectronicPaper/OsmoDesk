// Monitoring tools that run on the decoded frame in the browser.
//
// The server only has to deliver JPEGs. False colour, zebras, peaking, the
// scopes and the clip indicators are all per-pixel work a canvas does well at
// preview resolution, and doing them here keeps the decode pipeline free to
// concentrate on not dropping frames.
//
// Shared by the engineering panel and the cine monitor. Two copies of exposure
// maths would eventually disagree, and the one you were not looking at would
// be the one that was wrong.

(function (root) {
  "use strict";

  // Statistics, not detail: assists answer "is this exposed correctly", which
  // a 320x180 sample answers as well as a full frame and far more cheaply.
  const SW = 320, SH = 180;

  const samp = document.createElement("canvas");
  samp.width = SW; samp.height = SH;
  const sctx = samp.getContext("2d", { willReadFrequently: true });

  /** Pull luma and RGB planes from an <img>. Null if it is not ready. */
  function sample(img) {
    if (!img || !img.naturalWidth) return null;
    sctx.drawImage(img, 0, 0, SW, SH);
    let d;
    try { d = sctx.getImageData(0, 0, SW, SH).data; } catch (e) { return null; }
    const lum = new Uint8Array(SW * SH);
    const rgb = new Uint8Array(SW * SH * 3);
    for (let i = 0, p = 0; i < lum.length; i++, p += 4) {
      const r = d[p], g = d[p + 1], b = d[p + 2];
      rgb[i * 3] = r; rgb[i * 3 + 1] = g; rgb[i * 3 + 2] = b;
      lum[i] = (0.2126 * r + 0.7152 * g + 0.0722 * b) | 0;
    }
    return { lum, rgb };
  }

  // IRE-banded. Coarse on purpose: the point is judging exposure at a glance,
  // so the two bands that matter -- clipping and skin -- have to be obvious
  // without counting steps.
  function falseColour(y) {
    if (y >= 250) return [255, 0, 0];        // clipped
    if (y >= 235) return [255, 150, 0];      // near clip
    if (y >= 160) return [255, 255, 0];      // highlights
    if (y >= 130) return [0, 255, 0];        // about a stop over grey
    if (y >= 100) return [190, 190, 190];    // the 18% grey band
    if (y >= 60) return [0, 180, 255];       // shadows
    if (y >= 20) return [0, 60, 255];        // deep shadow
    return [140, 0, 255];                    // crushed
  }

  /** Paint false colour, zebras and peaking onto an overlay canvas. */
  function paintAssists(canvas, lum, on) {
    const any = on.falsecolor || on.zebra || on.peaking;
    if (!any) {
      if (canvas.width) {
        canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
      }
      return;
    }
    canvas.width = SW; canvas.height = SH;
    const ctx = canvas.getContext("2d");
    const out = ctx.createImageData(SW, SH);
    for (let i = 0, p = 0; i < lum.length; i++, p += 4) {
      const y = lum[i];
      let r = 0, g = 0, b = 0, a = 0;
      if (on.falsecolor) { const c = falseColour(y); r = c[0]; g = c[1]; b = c[2]; a = 235; }
      if (on.zebra && y >= 235 && ((i % SW) + Math.floor(i / SW)) % 8 < 3) {
        r = 255; g = 255; b = 255; a = 255;
      }
      out.data[p] = r; out.data[p + 1] = g; out.data[p + 2] = b; out.data[p + 3] = a;
    }
    if (on.peaking) {                        // gradient magnitude on luma
      for (let y = 1; y < SH - 1; y++) {
        for (let x = 1; x < SW - 1; x++) {
          const i = y * SW + x;
          const gx = lum[i + 1] - lum[i - 1], gy = lum[i + SW] - lum[i - SW];
          if (gx * gx + gy * gy > 3200) {
            const p2 = i * 4;
            out.data[p2] = 255; out.data[p2 + 1] = 40;
            out.data[p2 + 2] = 200; out.data[p2 + 3] = 255;
          }
        }
      }
    }
    ctx.putImageData(out, 0, 0);
  }

  function renderScope(canvas, name, lum, rgb) {
    if (!canvas) return;
    const g = canvas.getContext("2d");
    const W = canvas.width, H = canvas.height;
    g.fillStyle = "#05070a"; g.fillRect(0, 0, W, H);

    if (name === "histo") {
      // Per channel, overlaid, plus luma -- the RGBL a colourist expects.
      const chans = [[0, "#ff5a5a"], [1, "#5aff8c".replace("8c", "c8")], [2, "#5a9dff"]];
      const bins = [new Uint32Array(64), new Uint32Array(64), new Uint32Array(64)];
      const lbins = new Uint32Array(64);
      for (let i = 0; i < lum.length; i++) {
        bins[0][rgb[i * 3] >> 2]++;
        bins[1][rgb[i * 3 + 1] >> 2]++;
        bins[2][rgb[i * 3 + 2] >> 2]++;
        lbins[lum[i] >> 2]++;
      }
      let max = 1;
      for (const arr of bins.concat([lbins])) {
        for (let i = 0; i < 64; i++) if (arr[i] > max) max = arr[i];
      }
      const plot = (arr, colour) => {
        g.strokeStyle = colour; g.lineWidth = 1.2; g.beginPath();
        for (let i = 0; i < 64; i++) {
          const x = i * (W / 63), y = H - 4 - (arr[i] / max) * (H - 10);
          i ? g.lineTo(x, y) : g.moveTo(x, y);
        }
        g.stroke();
      };
      plot(bins[0], "#ff5a5a"); plot(bins[1], "#5affc8"); plot(bins[2], "#5a9dff");
      plot(lbins, "#f2f5f8");
    } else if (name === "wave") {
      const img = g.createImageData(W, H);
      for (let x = 0; x < W; x++) {
        const sx = Math.floor(x * SW / W);
        for (let y = 0; y < SH; y++) {
          const py = H - 1 - Math.floor(lum[y * SW + sx] * (H - 1) / 255);
          const o = (py * W + x) * 4;
          img.data[o] = 80; img.data[o + 1] = 210; img.data[o + 2] = 130;
          img.data[o + 3] = Math.min(255, (img.data[o + 3] || 0) + 70);
        }
      }
      g.putImageData(img, 0, 0);
      clipLines(g, W, H);
    } else if (name === "parade") {
      const img = g.createImageData(W, H);
      const third = Math.floor(W / 3);
      for (let ch = 0; ch < 3; ch++) {
        for (let x = 0; x < third; x++) {
          const sx = Math.floor(x * SW / third);
          for (let y = 0; y < SH; y++) {
            const py = H - 1 - Math.floor(rgb[(y * SW + sx) * 3 + ch] * (H - 1) / 255);
            const o = (py * W + ch * third + x) * 4;
            img.data[o + ch] = 230;
            img.data[o + 3] = Math.min(255, (img.data[o + 3] || 0) + 70);
          }
        }
      }
      g.putImageData(img, 0, 0);
      clipLines(g, W, H);
    } else if (name === "vector") {
      const R = Math.min(W, H) / 2 - 4;
      g.strokeStyle = "#26323d"; g.lineWidth = 1;
      g.beginPath(); g.arc(W / 2, H / 2, R, 0, Math.PI * 2); g.stroke();
      g.beginPath(); g.arc(W / 2, H / 2, R * 0.5, 0, Math.PI * 2); g.stroke();
      g.beginPath(); g.moveTo(W / 2 - R, H / 2); g.lineTo(W / 2 + R, H / 2);
      g.moveTo(W / 2, H / 2 - R); g.lineTo(W / 2, H / 2 + R); g.stroke();
      // Skin-tone line: the one graticule a camera operator actually uses.
      g.strokeStyle = "#c8a06e"; g.setLineDash([3, 3]);
      g.beginPath(); g.moveTo(W / 2, H / 2);
      g.lineTo(W / 2 + R * Math.cos(-2.44), H / 2 + R * Math.sin(-2.44));
      g.stroke(); g.setLineDash([]);
      g.fillStyle = "rgba(120,220,255,.55)";
      for (let i = 0; i < rgb.length; i += 3 * 7) {   // decimated: shape, not census
        const r = rgb[i], gg = rgb[i + 1], b = rgb[i + 2];
        const u = -0.169 * r - 0.331 * gg + 0.5 * b;
        const v = 0.5 * r - 0.419 * gg - 0.081 * b;
        g.fillRect(W / 2 + (u / 128) * R, H / 2 - (v / 128) * R, 1.4, 1.4);
      }
    }
  }

  // Broadcast legal marks, so a waveform is read against something.
  function clipLines(g, W, H) {
    g.strokeStyle = "rgba(255,80,80,.5)"; g.setLineDash([4, 4]); g.lineWidth = 1;
    for (const frac of [0.06, 0.94]) {
      const y = H - H * frac;
      g.beginPath(); g.moveTo(0, y); g.lineTo(W, y); g.stroke();
    }
    g.setLineDash([]);
  }

  /**
   * Per-channel clipping census, for the traffic-light strip.
   *
   * Returns the fraction of pixels crushed and blown on each channel. A
   * waveform tells you a channel is clipping somewhere; this tells you at a
   * glance which one and how much, which is the question you ask while the
   * camera is still rolling.
   */
  function trafficLights(rgb) {
    const n = rgb.length / 3;
    const hi = [0, 0, 0], lo = [0, 0, 0], sum = [0, 0, 0];
    for (let i = 0; i < n; i++) {
      for (let c = 0; c < 3; c++) {
        const v = rgb[i * 3 + c];
        sum[c] += v;
        if (v >= 253) hi[c]++;
        else if (v <= 2) lo[c]++;
      }
    }
    return {
      high: hi.map(v => v / n),
      low: lo.map(v => v / n),
      mean: sum.map(v => v / n / 255),
    };
  }

  // ---------------------------------------------------------------------
  // Monitor LUT
  //
  // A viewing look: it changes what the operator sees and nothing the camera
  // records. That distinction is the whole reason a viewing LUT exists -- you
  // shoot flat and judge graded -- and the UI has to keep saying it, because
  // an operator who thinks the look is baked in will light for the wrong
  // picture.
  //
  // This is the one job on this page that genuinely wants the GPU. A 3D LUT
  // is a per-pixel trilinear lookup; on a 960x540 preview that is half a
  // million interpolations a frame, which is tens of milliseconds in
  // JavaScript and a rounding error in a shader. WebGL2 samples a 3D texture
  // natively, so the shader is short and there is no tile-packing to get
  // subtly wrong.
  //
  // The table is uploaded as 8-bit. A monitor LUT is a preview, hardware LUT
  // boxes are 8- or 10-bit at the output stage anyway, and float 3D textures
  // need a linear-filtering extension that is not universally present. Deep
  // shadows on a heavy log-to-Rec709 look can band slightly. That is a
  // preview artefact and is not in the recording.

  const LUT_VERT = `#version 300 es
    in vec2 aPos;
    out vec2 vUv;
    void main() {
      vUv = vec2(aPos.x * 0.5 + 0.5, 0.5 - aPos.y * 0.5);
      gl_Position = vec4(aPos, 0.0, 1.0);
    }`;

  const LUT_FRAG = `#version 300 es
    precision highp float;
    precision highp sampler3D;
    uniform sampler2D uFrame;
    uniform sampler3D uLut;
    uniform float uSize;
    uniform float uAmount;
    uniform float uExposure;
    uniform vec3 uDomainMin;
    uniform vec3 uDomainMax;
    in vec2 vUv;
    out vec4 frag;
    void main() {
      vec4 src = texture(uFrame, vUv);
      // Exposure first. A log look expects to be fed correctly exposed log,
      // so the compensation belongs before the table, not after it -- after
      // would just brighten an already graded picture.
      vec3 lit = clamp(src.rgb * exp2(uExposure), 0.0, 1.0);
      vec3 c = clamp((lit - uDomainMin) / (uDomainMax - uDomainMin), 0.0, 1.0);
      // Half-texel inset: sampling at 0 and 1 exactly would read half a texel
      // outside the cube and clamp, flattening both ends of the curve.
      vec3 uvw = (c * (uSize - 1.0) + 0.5) / uSize;
      frag = vec4(mix(lit, texture(uLut, uvw).rgb, uAmount), src.a);
    }`;

  function compile(gl, type, src) {
    const sh = gl.createShader(type);
    gl.shaderSource(sh, src.trim());
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
      const log = gl.getShaderInfoLog(sh);
      gl.deleteShader(sh);
      throw new Error("shader: " + log);
    }
    return sh;
  }

  /**
   * Applies a .cube table to frames on a canvas.
   *
   * Construct it, check `.supported`, then `setTable()` and `draw()`. Every
   * failure path leaves `.supported` false and `.reason` set, because a look
   * that silently does not apply is worse than no look at all -- the operator
   * believes they are judging the graded picture.
   */
  function LutView(canvas) {
    this.canvas = canvas;
    this.supported = false;
    this.reason = "";
    this.amount = 1;
    this.exposure = 0;
    this._size = 0;

    const gl = canvas.getContext("webgl2", {
      preserveDrawingBuffer: false, alpha: false, antialias: false,
    });
    if (!gl) {
      this.reason = "this browser has no WebGL2";
      return;
    }
    this.gl = gl;

    try {
      const prog = gl.createProgram();
      gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, LUT_VERT));
      gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, LUT_FRAG));
      gl.linkProgram(prog);
      if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
        throw new Error("link: " + gl.getProgramInfoLog(prog));
      }
      this.prog = prog;
    } catch (err) {
      this.reason = String(err.message || err);
      return;
    }

    gl.useProgram(this.prog);
    this.u = {
      frame: gl.getUniformLocation(this.prog, "uFrame"),
      lut: gl.getUniformLocation(this.prog, "uLut"),
      size: gl.getUniformLocation(this.prog, "uSize"),
      amount: gl.getUniformLocation(this.prog, "uAmount"),
      exposure: gl.getUniformLocation(this.prog, "uExposure"),
      dmin: gl.getUniformLocation(this.prog, "uDomainMin"),
      dmax: gl.getUniformLocation(this.prog, "uDomainMax"),
    };

    // One full-screen triangle. Two triangles would need a diagonal seam that
    // some drivers rasterise with a visible crack.
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER,
                  new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(this.prog, "aPos");
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

    this.frameTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.frameTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);

    this.lutTex = gl.createTexture();
    this.supported = true;
  }

  /**
   * Upload a table parsed by the host. `table` is the /api/lut payload:
   * {size, dimensions, data, domain_min, domain_max}.
   */
  LutView.prototype.setTable = function (table) {
    if (!this.supported) return false;
    const gl = this.gl;
    if (!table) { this._size = 0; return true; }
    if (table.dimensions !== 3) {
      this.reason = "only 3D tables are applied on the monitor";
      return false;
    }
    const n = table.size;
    const want = n * n * n * 3;
    if (!table.data || table.data.length !== want) {
      // The host validates this too. Checked again here because uploading a
      // short buffer to a 3D texture is undefined behaviour, not an error.
      this.reason = "table is " + (table.data ? table.data.length : 0) +
                    " values, expected " + want;
      return false;
    }

    const bytes = new Uint8Array(n * n * n * 4);
    for (let i = 0, j = 0; i < want; i += 3, j += 4) {
      bytes[j] = Math.max(0, Math.min(255, Math.round(table.data[i] * 255)));
      bytes[j + 1] = Math.max(0, Math.min(255, Math.round(table.data[i + 1] * 255)));
      bytes[j + 2] = Math.max(0, Math.min(255, Math.round(table.data[i + 2] * 255)));
      bytes[j + 3] = 255;
    }

    gl.bindTexture(gl.TEXTURE_3D, this.lutTex);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    // Clamp on every axis. Wrapping a LUT sends the brightest highlight to
    // the darkest shadow, which is unmistakable but only after it is on set.
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_3D, gl.TEXTURE_WRAP_R, gl.CLAMP_TO_EDGE);
    gl.texImage3D(gl.TEXTURE_3D, 0, gl.RGBA8, n, n, n, 0,
                  gl.RGBA, gl.UNSIGNED_BYTE, bytes);

    this._size = n;
    this._dmin = table.domain_min || [0, 0, 0];
    this._dmax = table.domain_max || [1, 1, 1];
    return true;
  };

  LutView.prototype.hasTable = function () { return this._size > 0; };

  /** Draw `source` (an <img> or canvas) through the look. */
  LutView.prototype.draw = function (source, w, h) {
    if (!this.supported || !this._size || !source) return false;
    const gl = this.gl;
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w; this.canvas.height = h;
    }
    gl.viewport(0, 0, w, h);
    gl.useProgram(this.prog);

    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.frameTex);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    try {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
    } catch (err) {
      // A frame that is mid-decode throws rather than returning null.
      return false;
    }
    gl.uniform1i(this.u.frame, 0);

    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_3D, this.lutTex);
    gl.uniform1i(this.u.lut, 1);

    gl.uniform1f(this.u.size, this._size);
    gl.uniform1f(this.u.amount, this.amount);
    gl.uniform1f(this.u.exposure, this.exposure);
    gl.uniform3fv(this.u.dmin, this._dmin);
    gl.uniform3fv(this.u.dmax, this._dmax);

    gl.drawArrays(gl.TRIANGLES, 0, 3);
    return true;
  };

  root.Monitor = {
    SW, SH, sample, falseColour, paintAssists, renderScope, trafficLights,
    LutView,
  };
})(window);
