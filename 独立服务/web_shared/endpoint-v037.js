/* Foreground pause detector, not a speech recognizer. Air Silero still gates ASR. */
(function (root) {
  'use strict';
  class EndpointGate {
    constructor({silenceMs = 2000, speechMs = 180, calibrationMs = 200} = {}) {
      this.silenceMs = silenceMs; this.speechMs = speechMs; this.calibrationMs = calibrationMs;
      this.elapsed = 0; this.candidate = 0; this.quiet = 0; this.last = null;
      this.samples = []; this.noise = .001; this.heard = false; this.done = false;
      this.startupVoicedMs = 0; this.startupFloor = Infinity; this.startupLevel = 0;
    }
    step(rms, now) {
      if (!Number.isFinite(rms) || rms < 0 || !Number.isFinite(now) || this.done) return null;
      if (this.last !== null && now <= this.last) return null;
      const dt = this.last === null ? 0 : Math.min(100, now - this.last);
      this.last = now; this.elapsed += dt;
      if (this.elapsed <= this.calibrationMs) {
        // Preserve sustained sound during calibration: the user may speak at once.
        // A single click must not count as an already-started utterance.
        if (rms >= .003) {
          this.startupVoicedMs += dt;
          this.startupFloor = Math.min(this.startupFloor, rms);
          if (this.startupVoicedMs >= this.speechMs) this.startupLevel = this.startupFloor;
        } else { this.startupVoicedMs = 0; this.startupFloor = Infinity; }
        this.samples.push(rms);
        const sorted = this.samples.slice().sort((a, b) => a - b);
        this.noise = Math.max(.0004, sorted[Math.floor((sorted.length - 1) * .2)]);
        return null;
      }
      if (!this.heard) {
        // A sustained opening sound followed by a clear drop is evidence that
        // calibration contained speech. Recover it without increasing the
        // normal silence threshold (which could cut off a quieter sentence tail).
        if (this.startupLevel && rms < Math.min(.0015, this.startupLevel * .25)) {
          this.heard = true;
          this.noise = Math.max(.0004, rms);
          this.quiet = dt;
          return null;
        }
        // Speech may begin immediately: let a later quiet frame lower calibration.
        if (rms < this.noise) this.noise = Math.max(.0004, rms);
        const high = Math.max(.003, this.noise * 3);
        this.candidate = rms >= high ? this.candidate + dt : 0;
        if (this.candidate >= this.speechMs) { this.heard = true; this.quiet = 0; }
        return null;
      }
      const low = Math.max(.0015, this.noise * 1.6);
      this.quiet = rms < low ? this.quiet + dt : 0;
      if (this.quiet >= this.silenceMs) { this.done = true; return 'end'; }
      return null;
    }
  }
  root.PiEndpointGate = EndpointGate;
  if (typeof module !== 'undefined') module.exports = EndpointGate;
})(typeof window === 'undefined' ? globalThis : window);
