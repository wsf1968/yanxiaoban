/* Diagnostic capture -> PCM -> Air -> audible transcript. No business actions. */
(function (root) {
  'use strict';
  function wav16(samples) {
    if (!samples.length || samples.length > 480000) throw new Error('录音长度不支持');
    const out = new ArrayBuffer(44 + samples.length * 2), v = new DataView(out);
    const str = (at, s) => [...s].forEach((c, i) => v.setUint8(at + i, c.charCodeAt(0)));
    str(0, 'RIFF'); v.setUint32(4, out.byteLength - 8, true); str(8, 'WAVE');
    str(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
    v.setUint16(22, 1, true); v.setUint32(24, 16000, true); v.setUint32(28, 32000, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, 'data');
    v.setUint32(40, samples.length * 2, true);
    samples.forEach((s, i) => {
      if (!Number.isFinite(s)) throw new Error('录音数据无效');
      const x = Math.max(-1, Math.min(1, s));
      v.setInt16(44 + i * 2, Math.round(x * (x < 0 ? 32768 : 32767)), true);
    });
    return out;
  }
  function bounded(promise, ms, label) {
    let timer;
    return Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(label)), ms);
    })]).finally(() => clearTimeout(timer));
  }
  class VoiceProbe {
    constructor(endpoint, notify, signal = () => {}, options = {}) {
      this.endpoint = endpoint; this.notify = notify; this.signal = signal; this.rev = 0;
      this.phase = 'idle'; this.audio = null; this.pendingCapture = null; this.retryAvailable = false;
      this.continuous = false; this.round = 0;
      this.entryAttempted = false;
      this.welcomeUrl = options.welcomeUrl || null;
      this.onResult = options.onResult || (() => {});
      this.onProgress = options.onProgress || (() => {});
    }
    status(phase, message, text) {
      this.phase = phase; this.notify({phase, message, text});
    }
    cleanup() {
      clearTimeout(this.timer);
      clearTimeout(this.nextTimer);
      clearInterval(this.waitTimer);
      clearTimeout(this.processingDeadline);
      this.stopMeter();
      if (this.welcomeController) this.welcomeController.abort();
      if (this.welcomeCancel) this.welcomeCancel();
      if (this.controller) this.controller.abort();
      if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop();
      if (this.stream) this.stream.getTracks().forEach(t => t.stop());
      if (this.source) { this.source.onended = null; try { this.source.stop(); } catch (_) {} }
      this.stream = null; this.recorder = null; this.source = null;
    }
    stop() {
      this.continuous = false;
      ++this.rev; this.cleanup();
      if (this.ctx) this.ctx.suspend().catch(() => {});
      this.status('stopped', this.audio ? '已停止；可点“重听回复”。' : '已停止。');
    }
    async enter() {
      if (this.entryAttempted) return;
      this.entryAttempted = true;
      if (root.document && root.document.hidden) {
        this.status('stopped', '页面在后台；回到页面后点“开始新对话”。');
        return;
      }
      return this.start(false, true);
    }
    async start(automatic = false, entry = false, resume = null) {
      if (automatic && !this.continuous) return;
      if (automatic && root.document && root.document.hidden) { this.stop(); return; }
      ++this.rev; this.cleanup(); this.audio = null; this.pendingCapture = null; this.retryAvailable = false;
      const rev = this.rev; this.captureLimited = false;
      const AC = root.AudioContext || root.webkitAudioContext;
      try {
        if (!automatic) {
          this.continuous = true; this.round = 0;
          if (!root.crypto || typeof root.crypto.randomUUID !== 'function') throw new Error('请使用Safari打开Pi的HTTPS试用链接');
          if (resume && (!/^[a-f0-9]{32}$/.test(resume.id) || !Number.isInteger(resume.revision) || resume.revision < 0)) throw new Error('这段对话已结束，请开始新对话。');
          this.conversationId = resume ? resume.id : root.crypto.randomUUID().replace(/-/g, '');
          this.conversationRevision = resume ? resume.revision : 0;
        }
        if (!AC || !root.navigator.mediaDevices || !root.MediaRecorder) throw new Error('当前浏览器不支持录音');
        if (!this.ctx || this.ctx.state === 'closed') this.ctx = new AC();
        // Resume synchronously from the tap; separate output context survives mic close.
        // A tap can unlock playback immediately. On page entry, obtain the
        // microphone first: a pending autoplay resume must not block capture.
        const resumed = entry ? Promise.resolve() : this.ctx.resume();
        resumed.catch(() => {}); // Permission can take longer; observe early failures now.
        this.status('permission', entry ? '正在自动接管语音；如有提示，请允许麦克风。' :
          automatic ? '准备继续听……' : '请允许麦克风，随后开始说话。');
        const mic = root.navigator.mediaDevices.getUserMedia({audio: true});
        mic.then(stream => { if (rev !== this.rev) stream.getTracks().forEach(t => t.stop()); }, () => {});
        const stream = await bounded(mic, 20000, '等待麦克风超时，请重试');
        if (rev !== this.rev) return;
        this.stream = stream;
        await bounded(resumed, 5000, '声音通道启动超时');
        if (rev !== this.rev) return;
        // iOS can interrupt the context when a Bluetooth microphone changes
        // the audio route, after the earlier resume promise already resolved.
        if (this.ctx.state !== 'running') {
          await bounded(this.ctx.resume(), 5000, '耳机收音通道恢复超时，请重新开始');
          if (rev !== this.rev) return;
          if (this.ctx.state !== 'running') throw new Error('耳机收音通道尚未恢复，请重新开始');
        }
        // Say hello before starting MediaRecorder or the endpoint detector.
        if (!automatic && this.welcomeUrl) {
          if (!await this.sayWelcome(rev) || rev !== this.rev) return;
        }
        const recorder = new root.MediaRecorder(stream); this.recorder = recorder;
        const chunks = []; let bytes = 0;
        recorder.ondataavailable = e => {
          if (rev !== this.rev || !e.data.size) return;
          bytes += e.data.size;
          chunks.push(e.data);
          if (bytes > 8 * 1024 * 1024 && this.phase === 'recording') { this.captureLimited = true; this.finish(); }
        };
        recorder.onerror = () => {
          if (rev === this.rev) { this.stop(); this.status('error', '录音失败，请重试。'); }
        };
        recorder.onstop = () => {
          if (rev === this.rev) this.stopMeter();
          stream.getTracks().forEach(t => t.stop());
          if (rev === this.rev) this.process(new Blob(chunks, {type: recorder.mimeType}), rev);
        };
        recorder.start(250);
        this.round += 1;
        this.status('recording', '正在听第' + this.round + '段，你可以自然说话，说完后稍作停顿。');
        this.watchPause(stream, rev);
        // Silence can release an unused microphone; ongoing speech has no wall-clock cutoff.
        this.timer = setTimeout(() => {
          if (rev !== this.rev || this.phase !== 'recording' || this.gate?.heard) return;
          this.stop(); this.status('stopped','暂未听到说话，已暂停收音。准备好后可继续。');
        }, 60000);
      } catch (e) {
        if (rev !== this.rev) return;
        this.continuous = false; ++this.rev; this.cleanup();
        if (entry && this.ctx) this.ctx.suspend().catch(() => {});
        this.status('error', entry ? '自动接管未成功，请点“开始新对话”继续。' : e.message || '录音启动失败');
      }
    }
    async sayWelcome(rev) {
      this.status('permission', '正在准备启动招呼语……');
      const controller = new AbortController(); this.welcomeController = controller;
      let buffer;
      try {
        const response = await bounded(fetch(this.welcomeUrl, {signal:controller.signal,
          credentials:'same-origin'}), 5000, '启动招呼语加载超时，请重新开始');
        if (!response.ok) throw new Error('启动招呼语未能加载，请重新开始');
        const raw = await bounded(response.arrayBuffer(), 5000, '启动招呼语读取超时');
        if (raw.byteLength > 300000) throw new Error('启动招呼语格式异常');
        if (rev !== this.rev) return false;
        buffer = await bounded(this.ctx.decodeAudioData(raw), 5000, '启动招呼语解码超时');
      } finally {
        controller.abort();
        if (this.welcomeController === controller) this.welcomeController = null;
      }
      if (rev !== this.rev) return false;
      if (!(buffer.duration > 0 && buffer.duration <= 5)) throw new Error('启动招呼语长度异常');
      if (this.ctx.state !== 'running') throw new Error('请点“开始新对话”启用声音');
      const source = this.ctx.createBufferSource(); this.source = source;
      source.buffer = buffer; source.connect(this.ctx.destination);
      let resolve;
      const ended = new Promise(done => { resolve = done; });
      const cancel = () => {
        source.onended = null;
        try { source.stop(); source.disconnect(); } catch (_) {}
        if (this.source === source) this.source = null;
        resolve(false);
      };
      this.welcomeCancel = cancel;
      source.onended = () => resolve(true);
      try {
        this.status('permission', '你好，我在，请说。（招呼语播完后开始收音）');
        source.start();
        return await bounded(ended, Math.ceil(buffer.duration * 1000) + 1500, '启动招呼语播放未完成，请重新开始');
      } finally {
        cancel();
        if (this.welcomeCancel === cancel) this.welcomeCancel = null;
      }
    }
    stopMeter() {
      clearInterval(this.meterTimer);
      for (const node of [this.micNode, this.analyser, this.analysisSink]) if (node) { try { node.disconnect(); } catch (_) {} }
      this.micNode = null; this.analyser = null; this.analysisSink = null;
    }
    reportCapture(reason) {
      const trace = this.captureTrace;
      if (!trace || trace.reported || !trace.frames.length || !/^[0-9a-f]{12}$/.test(trace.id)) return;
      trace.reported = true;
      const context = this.ctx && this.ctx.state;
      const body = JSON.stringify({version:'0.3.4', id:trace.id, round:trace.round, reason,
        context:['running','suspended','interrupted','closed'].includes(context) ? context : 'unknown',
        frames:trace.frames});
      if (body.length > 16384) return;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 5000);
      // Diagnostic delivery is independent of the dialogue. No UI transition,
      // audio/text content, retries, or model requests; failure cannot block stop.
      try {
        Promise.resolve(fetch(this.endpoint, {method:'POST', credentials:'same-origin',
          headers:{'Content-Type':'application/json','X-PI-Request':'1','X-Pi-Diagnostic':'capture-1'},
          body, signal:controller.signal})).catch(() => {}).finally(() => clearTimeout(timer));
      } catch (_) { clearTimeout(timer); }
    }
    watchPause(stream, rev) {
      if (!root.PiEndpointGate) throw new Error('自动停顿模块没有加载，请刷新页面');
      this.gate = new root.PiEndpointGate();
      this.micNode = this.ctx.createMediaStreamSource(stream);
      this.analyser = this.ctx.createAnalyser(); this.analyser.fftSize = 2048;
      // Explicitly pull the analysis graph while recording. Microphone output
      // is muted BEFORE connecting; this is not background keep-alive audio.
      this.analysisSink = this.ctx.createGain(); this.analysisSink.gain.value = 0;
      this.micNode.connect(this.analyser);
      this.analyser.connect(this.analysisSink); this.analysisSink.connect(this.ctx.destination);
      const buffer = new Float32Array(2048);
      let frames = 0, peak = 0, recovering = false, recoveryAttempts = 0;
      const started = root.performance.now();
      this.captureTrace = {id:(this.conversationId || '').slice(0,12), round:this.round,
        frames:[], peak:0, reported:false};
      this.meterTimer = setInterval(() => {
        if (rev !== this.rev || this.phase !== 'recording') return;
        if (root.document && root.document.hidden) { this.stop(); return; }
        try {
          if (this.ctx.state !== 'running') {
            this.signal({rms:0, peak, context:this.ctx.state, heard:this.gate.heard, quiet:0, frames});
            if (!recovering) {
              if (recoveryAttempts++) { this.stop(); this.status('error', '耳机声音通道反复暂停，请重新开始。'); return; }
              recovering = true;
              bounded(this.ctx.resume(), 3000, '收音通道恢复超时').then(() => {
                if (rev !== this.rev) return;
                recovering = false;
                if (this.ctx.state !== 'running') { this.stop(); this.status('error', '收音通道已暂停，请重新开始。'); }
              }).catch(() => { if (rev === this.rev) { this.stop(); this.status('error', '收音通道恢复失败，请重新开始。'); } });
            }
            return;
          }
          this.analyser.getFloatTimeDomainData(buffer);
          let sum = 0; for (const sample of buffer) sum += sample * sample;
          const rms = Math.sqrt(sum / buffer.length); peak = Math.max(peak, rms); frames++;
          const now = root.performance.now();
          const ended = this.gate.step(rms, now) === 'end';
          const trace = this.captureTrace;
          trace.peak = peak;
          if (frames % 2 === 0 && trace.frames.length < 250 && now - started <= 30000) {
            trace.frames.push([Math.round(now-started), +rms.toFixed(6), +this.gate.noise.toFixed(6),
              Math.round(this.gate.candidate), Math.round(this.gate.quiet), this.gate.heard ? 1 : 0]);
          }
          if (frames % 5 === 0 || ended) this.signal({rms, peak, context:this.ctx.state,
            heard:this.gate.heard, quiet:this.gate.quiet, frames});
          if (ended) this.finish();
        } catch (_) { this.stop(); this.status('error', '自动收音中断，请重新开始。'); }
      }, 50);
    }
    continueAfterPlayback(rev) {
      if (!this.continuous || rev !== this.rev) return;
      this.nextTimer = setTimeout(() => {
        if (rev === this.rev && this.continuous) this.start(true);
      }, 600);
    }
    finish() {
      if (this.phase !== 'recording') return;
      clearTimeout(this.timer);
      this.stopMeter();
      this.status('processing', '正在理解，请稍候。');
      this.recorder.stop();
    }
    async process(blob, rev) {
      const began = Date.now();
      const capture=this.pendingCapture && this.pendingCapture.blob===blob ? this.pendingCapture : {
        blob, id:this.conversationId, revision:this.conversationRevision,
        requestId:root.crypto.randomUUID().replace(/-/g,''), limited:this.captureLimited};
      this.pendingCapture=capture; this.retryAvailable=false;
      const waitTimer = setInterval(() => {
        if (rev !== this.rev || this.phase !== 'processing') return;
        const seconds = Math.floor((Date.now() - began) / 1000);
        this.status('processing', seconds < 15 ? '正在等待Pi回复，已等待' + seconds + '秒。' :
          '正在处理这段录音，已等待' + seconds + '秒；录音已保留，可随时停止。');
      }, 1000);
      this.waitTimer = waitTimer;
      try {
        this.status('processing', '正在准备这句话的录音……');
        const rawRecording = await bounded(blob.arrayBuffer(), 5000, '读取录音超时，请重新开始');
        if (rev !== this.rev) return;
        const decoded = await bounded(this.ctx.decodeAudioData(rawRecording), 10000, '录音解码超时');
        if (rev !== this.rev) return;
        if (!decoded.duration) throw new Error('录音为空');
        const Offline = root.OfflineAudioContext || root.webkitOfflineAudioContext;
        const offline = new Offline(1, Math.ceil(decoded.duration * 16000), 16000);
        const source = offline.createBufferSource(); source.buffer = decoded;
        source.connect(offline.destination); source.start();
        const converted = await bounded(offline.startRendering(), 10000, '录音转换超时');
        if (rev !== this.rev) return;
        const requestId=capture.requestId;
        const response=await this.sendSegments(converted.getChannelData(0),capture,rev);
        if (rev !== this.rev) return;
        if (response.state === 'no_speech') {
          this.continuous = false; this.pendingCapture=null;
          this.status('idle', response.message + ' 点“开始新对话”重试。'); return;
        }
        if (response.id !== capture.id || response.requestId !== requestId ||
            response.revision !== capture.revision + 1 || typeof response.spoken !== 'string' ||
            !(response.taskExecuted === false || (response.taskExecuted === true && response.queryStatus === 'complete' && ((['bookkeeping','secretary'].includes(response.tool) && response.commercial?.kind===response.tool && response.commercial?.verified===true) || ['web_search','travel','weather','route','traffic','rail','amap'].includes(response.tool) || (response.tool==='shopping' && response.commercial?.kind==='shopping' && Array.isArray(response.commercial.products)) || (['xiaohongshu','flight'].includes(response.tool) && response.research?.completed === true) || (['taobao_instant','meituan','jd'].includes(response.tool) && response.shopping?.completed === true) || (response.tool === 'wechat' && response.wechat?.state === 'sent'))))) throw new Error('对话回复不匹配，请重新开始');
        this.conversationRevision = response.revision;
        try { this.onResult(response); } catch (_) { /* Presentation must not prevent audio delivery. */ }
        if (response.state === 'text_only') {
          this.continuous = false; this.retryAvailable=true;
          this.status('error', response.message+' 可重试这段录音恢复声音。', '你：' + response.text + '\nPi：' + response.spoken);
          return;
        }
        if (response.state !== 'ready' || response.mime !== 'audio/wav' || typeof response.text !== 'string' ||
            typeof response.audio !== 'string' || response.audio.length > 8000000) throw new Error('语音回复格式无效');
        const raw = Uint8Array.from(atob(response.audio), c => c.charCodeAt(0));
        const buffer = await bounded(this.ctx.decodeAudioData(raw.buffer), 10000, '回复声音解码超时');
        if (rev !== this.rev) return;
        this.audio = {buffer, text: '你：' + response.text + '\nPi：' + response.spoken + (Array.isArray(response.sources) ? response.sources.map(x => '\n来源：' + (x.title || x.provider || '') + (x.url ? ' ' + x.url : '') + (x.reporttime ? ' '+x.reporttime : '')).join('') : '')};
        this.pendingCapture=null;this.retryAvailable=false;
        this.status('ready', '回复已生成，准备播放。', this.audio.text);
        if (this.ctx.state === 'running') this.play(rev);
        else { this.continuous = false; this.status('ready', '声音通道暂停，请点“重听回复”。', this.audio.text); }
      } catch (e) {
        if (rev === this.rev) { this.continuous = false; this.retryAvailable=true; this.status('error', (e.message || '语音处理失败')+'。录音已保留，可以直接重试，不用重说。'); }
      } finally {
        clearInterval(waitTimer);
        if (this.waitTimer === waitTimer) this.waitTimer = null;
      }
    }
    async sendSegments(samples,capture,rev) {
      const chunkSize=25*16000,count=Math.ceil(samples.length/chunkSize);
      let response;
      for(let part=0;part<count;part++) {
        if(rev!==this.rev)throw new Error('已停止');
        const body=wav16(samples.subarray(part*chunkSize,Math.min(samples.length,(part+1)*chunkSize)));
        this.status('processing',count>1?`正在识别第${part+1}/${count}段；完整需求识别后再交给言小办。`:'正在理解这句话……');
        for(let attempt=0;attempt<2;attempt++) {
          const controller=new AbortController();this.controller=controller;
          try {
            response=await bounded((async()=>{
              const r=await fetch(this.endpoint,{method:'POST',credentials:'same-origin',signal:controller.signal,
                headers:{'Content-Type':'audio/wav','X-PI-Request':'1',...(root.PiPresentation?{'Accept':'application/x-ndjson'}:{}),'X-Pi-Conversation':capture.id,
                  'X-Pi-Revision':String(capture.revision),'X-Pi-Turn':capture.requestId,
                  'X-Pi-Segment':String(part),'X-Pi-Segments':String(count),
                  'X-Pi-Capture-End':capture.limited?'limit':'pause'},body});
              const data=root.PiPresentation?await root.PiPresentation.roundtrip(r,result=>{
                if(rev===this.rev&&result.id===capture.id&&result.requestId===capture.requestId&&result.revision===capture.revision+1)
                  this.onProgress(result);
              }):await r.json();
              if(!r.ok){const e=new Error(r.status===401||r.status===403?'请先连接言小办':data.error==='conversation_conflict'?'这段对话的状态已变化，请先查看最新结果':'这次语音请求暂未完成');e.status=r.status;throw e;}
              return data;
            })(),180000,'这一段处理暂时超时');
            break;
          } catch(e) {
            if(rev!==this.rev || attempt || (e.status && e.status<500))throw e;
            this.status('processing','正在恢复这段录音的处理，不会重复提交任务。');
          } finally {controller.abort();}
        }
        if(part<count-1 && (response.state!=='segment_received'||response.segment!==part||response.taskExecuted!==false))throw new Error('录音分段回执不匹配');
      }
      return response;
    }
    async retryLast() {
      const saved=this.pendingCapture;
      if(!saved || !this.retryAvailable)return;
      if(saved.id!==this.conversationId || this.conversationRevision>saved.revision+1){this.status('error','对话已更新，请查看历史结果；旧录音不会覆盖新对话。');return;}
      ++this.rev;this.cleanup();const rev=this.rev;this.continuous=true;
      await this.ctx.resume();this.status('processing','正在重试已保留的录音……');
      return this.process(saved.blob,rev);
    }
    play(rev) {
      if (rev !== this.rev || !this.audio) return;
      if (this.source) { this.source.onended = null; this.source.stop(); }
      const source = this.ctx.createBufferSource(); this.source = source;
      source.buffer = this.audio.buffer; source.connect(this.ctx.destination);
      source.onended = () => { if (rev === this.rev && this.source === source) {
        this.source = null; this.status('finished', this.continuous ? '播放结束，准备继续听。' : 'Pi回复播放结束。', this.audio.text);
        this.continueAfterPlayback(rev);
      } };
      source.start(); this.status('playing', '正在播放Pi回复。', this.audio.text);
    }
    async replay() {
      if (!this.audio || ['recording', 'permission', 'processing'].includes(this.phase)) return;
      this.continuous = false; clearTimeout(this.nextTimer);
      const rev = this.rev;
      try {
        await bounded(this.ctx.resume(), 5000, '声音通道启动超时');
        this.play(rev);
      } catch (e) { if (rev === this.rev) this.status('error', e.message); }
    }
  }
  root.PiVoiceProbe = {VoiceProbe, wav16};
  if (typeof module !== 'undefined') module.exports = {VoiceProbe, wav16};
})(typeof window === 'undefined' ? globalThis : window);
