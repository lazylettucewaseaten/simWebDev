const HOST = "com.simweb.host";
const $ = (id) => document.getElementById(id);
const host = (msg) => chrome.runtime.sendNativeMessage(HOST, msg);

function applyPreset() {
  [$("memMB").value, $("cpuRate").value, $("cores").value] = $("preset").value.split(",");
}
$("preset").onchange = applyPreset;
applyPreset();

$("launch").onclick = async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const res = await host({
    cmd: "launch",
    url: tab.url,
    memMB: +$("memMB").value,
    cpuRate: +$("cpuRate").value,
    cores: +$("cores").value,
  });
  if (res.error) $("stats").textContent = res.error;
};
$("stop").onclick = () => host({ cmd: "stop" });

async function poll() {
  try {
    const s = await host({ cmd: "stats" });
    $("stats").textContent = s.error ? s.error : !s.running ? "Not running" :
      s.measuring ? "Measuring browser overhead..." :
      `Page RAM: ${s.pageMB} / ${s.budgetMB} MB\n` +
      `Browser overhead: ${s.overheadMB} MB (excluded)\n` +
      `Total: ${s.usedMB} MB\n` +
      `Tabs killed (OOM): ${s.oomKills}`;
  } catch (e) {
    $("stats").textContent = "Host not installed: run install.sh\n" + e.message;
  }
}
poll();
setInterval(poll, 1000);
