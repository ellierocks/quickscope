// Moonlight's default bitrate, to show on the settings page. The launcher
// works it out again at launch (moonlight_default_bitrate in
// quickscope_launcher.py); keep the two the same.

const PYROWAVE = 5;

// Moonlight's getDefaultBitrate(): [pixels, factor] points, interpolated.
const TABLE: [number, number][] = [
  [640 * 360, 1],
  [854 * 480, 2],
  [1280 * 720, 5],
  [1920 * 1080, 10],
  [2560 * 1440, 20],
  [3840 * 2160, 40],
];

// Nonary's fork, PyroWave: the PyroWave author's regression at his "good
// quality" level, for 4:2:0 and 4:4:4.
const PYROWAVE_420 = [
  611.9945665176643, 87.22375978784984, -374.2744854882103, 138.525763156332, 404.3879014230974, -64.04327593791751,
  -194.8035057145124, -12.904253458542499,
];
const PYROWAVE_444 = [
  719.659360640365, 51.9223145866537, -522.3229212980714, 104.10383129754959, 605.6153091742109, 87.26139899802612,
  -306.13390524230874, -110.39755862218644,
];

/** kbps, as Moonlight's "Use Default" picks it (the VRR fork's for PyroWave). */
export function defaultBitrate(
  width: number,
  height: number,
  fps: number,
  yuv444: boolean,
  codec: number,
  hdr: boolean,
) {
  let pixels = width * height;
  if (codec === PYROWAVE) {
    pixels = Math.min(Math.max(pixels, 1280 * 720), 3840 * 2160);
    const x = Math.sqrt(pixels * 1e-6) - 2;
    const estimate = (yuv444 ? PYROWAVE_444 : PYROWAVE_420).reduce((sum, c, i) => sum + c * x ** i, 0);
    const kbps = Math.trunc(estimate * 8e-3 * Math.max(fps, 1) * (hdr ? 1.2 : 1) * 1000);
    return Math.ceil(kbps / 5000) * 5000;
  }
  let factor: number;
  if (pixels <= TABLE[0][0]) factor = TABLE[0][1];
  else if (pixels >= TABLE[TABLE.length - 1][0]) factor = TABLE[TABLE.length - 1][1];
  else {
    const i = TABLE.findIndex(([p]) => pixels <= p);
    const [[p0, f0], [p1, f1]] = [TABLE[i - 1], TABLE[i]];
    factor = ((pixels - p0) / (p1 - p0)) * (f1 - f0) + f0;
  }
  if (yuv444) factor *= 2;
  // Not linear past 60 FPS.
  const frameRate = (fps <= 60 ? fps : Math.sqrt(fps / 60) * 60) / 30;
  return Math.floor(factor * frameRate + 0.5) * 1000;
}
