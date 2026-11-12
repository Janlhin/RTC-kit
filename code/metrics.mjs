// @ts-check
/**
 * metrics.mjs — 闹钟执行情况的历史账本（只读旁路）
 *
 * 数据来源只读：{{STATE_DIR}}/alarms.json
 * 数据落点只写：{{STATE_DIR}}/metrics.db（本脚本独占）
 *
 * 它不参与提醒链路。这个脚本挂了，闹钟照响。
 *
 * 用法：
 *   node metrics.mjs init                   建表
 *   node metrics.mjs sync                   今天 + 全部历史（幂等，最安全）
 *   node metrics.mjs sync today              只落今天的 4 行
 *   node metrics.mjs sync past               只补今天以前（prep 用）
 *   node metrics.mjs report [天数]            看汇总（默认 30 天）
 *   node metrics.mjs today                  只看今天一行
 *
 * 状态文件位置：
 *   --state <dir> 参数 > 环境变量 RTC_STATE > 脚本所在目录。
 *   放错目录时会明确列出尝试过的路径并退出，不会静默生成空账本。
 */

import { DatabaseSync } from "node:sqlite";
import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));

const argv = process.argv.slice(2);
const stateIdx = argv.indexOf("--state");
const STATE_ARG = stateIdx > -1 ? argv[stateIdx + 1] : null;
if (stateIdx > -1) argv.splice(stateIdx, 2);

const STATE_DIR = STATE_ARG || process.env.RTC_STATE || HERE;
const DB_PATH = join(STATE_DIR, "metrics.db");
const ALARMS_PATH = join(STATE_DIR, "alarms.json");

/**
 * @typedef {Object} Window
 * @property {string} id
 * @property {string} [label]
 * @property {number} expectFires
 * @property {number} fireCount
 * @property {string} state
 * @property {string|null} ackedAt
 * @property {string|null} missedReason
 */

/**
 * @typedef {Object} Alarms
 * @property {string} planDate
 * @property {Window[]} windows
 * @property {Array<{date:string,id:string,state:string,fires:number,ackedAt?:string|null}>} [history]
 */

// ---------- 基础设施 ----------

function open() {
  const db = new DatabaseSync(DB_PATH);
  db.exec(`
    CREATE TABLE IF NOT EXISTS daily (
      date      TEXT NOT NULL,
      window    TEXT NOT NULL,
      label     TEXT,
      expect    INTEGER NOT NULL,
      fired     INTEGER NOT NULL,
      acked_at  TEXT,
      missed    TEXT,
      PRIMARY KEY (date, window)
    );
    CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(date);
  `);
  return db;
}

function readAlarms() {
  if (!existsSync(ALARMS_PATH)) {
    throw new Error(
      `找不到 ${ALARMS_PATH}\n` +
        `  状态文件默认从脚本所在目录读取。用 --state <dir> 或环境变量 RTC_STATE 指定状态目录。`
    );
  }
  return /** @type {Alarms} */ (JSON.parse(readFileSync(ALARMS_PATH, "utf8")));
}

/**
 * 更新一天里的一行（幂等）
 * @param {DatabaseSync} db
 * @param {string} date
 * @param {string} window
 * @param {string} label
 * @param {number} expect
 * @param {number} fired
 * @param {string|null} ackedAt
 * @param {string|null} missed
 */
function upsert(db, date, window, label, expect, fired, ackedAt, missed) {
  db.prepare(
    `INSERT INTO daily (date, window, label, expect, fired, acked_at, missed)
     VALUES (?, ?, ?, ?, ?, ?, ?)
     ON CONFLICT(date, window) DO UPDATE SET
       label    = excluded.label,
       expect   = excluded.expect,
       fired    = excluded.fired,
       acked_at = excluded.acked_at,
       missed   = excluded.missed`
  ).run(date, window, label, expect, fired, ackedAt, missed);
}

// ---------- 命令 ----------

function cmdInit() {
  open().close();
  console.log(`已建表：${DB_PATH}`);
}

/** @param {string|undefined} dateArg */
function cmdSync(dateArg) {
  const db = open();
  const a = readAlarms();

  // 1) 当前计划日
  const target = dateArg || "all";
  let todayRows = 0;
  let histRows = 0;

  const wantToday = target === "all" || target === "today" || target === a.planDate;
  const wantPast = target === "all" || target === "past";

  if (wantToday) {
    for (const w of a.windows) {
      const missed = w.state === "missed" ? w.missedReason || "unknown" : null;
      upsert(db, a.planDate, w.id, w.label || w.id, w.expectFires, w.fireCount, w.ackedAt, missed);
      todayRows++;
    }
  }

  // 2) 历史（history 里没有 expect，用 6 作默认；label 从窗口表里找）
  const labelOf = new Map(a.windows.map((w) => [w.id, w.label || w.id]));
  const expectOf = new Map(a.windows.map((w) => [w.id, w.expectFires]));
  for (const h of a.history || []) {
    if (!wantPast) break;
    if (h.date >= a.planDate) continue;
    const missed = h.state === "missed" ? "history" : null;
    upsert(
      db,
      h.date,
      h.id,
      labelOf.get(h.id) || h.id,
      expectOf.get(h.id) || 6,
      h.fires,
      h.ackedAt ?? null,
      missed
    );
    histRows++;
  }

  db.close();
  const parts = [];
  if (todayRows) parts.push(`今日 ${a.planDate} ${todayRows} 行`);
  if (histRows) parts.push(`历史 ${histRows} 行`);
  console.log(`metrics: 写入 ${todayRows + histRows} 行（${parts.join("，")}）`);
}

/** @param {number} days */
function cmdReport(days) {
  const db = open();
  const since = new Date(Date.now() - days * 86400_000).toISOString().slice(0, 10);

  const overall = db
    .prepare(
      `SELECT COUNT(*) AS rows,
              COUNT(DISTINCT date) AS days,
              SUM(CASE WHEN acked_at IS NOT NULL THEN 1 ELSE 0 END) AS acked,
              SUM(CASE WHEN missed IS NOT NULL THEN 1 ELSE 0 END) AS missed,
              SUM(fired) AS fires,
              ROUND(AVG(CASE WHEN acked_at IS NOT NULL THEN fired END), 2) AS avg_fires_to_ack
         FROM daily WHERE date >= ?`
    )
    .get(since);

  if (!overall || !overall.rows) {
    console.log(`最近 ${days} 天没有数据。先跑：node metrics.mjs sync`);
    db.close();
    return;
  }

  const resolved = overall.acked + overall.missed;
  const rate = resolved ? overall.acked / resolved : 0;
  console.log(`周期：最近 ${days} 天（有数据 ${overall.days} 天）`);
  console.log(
    `确认率（按已结算口径）：${(rate * 100).toFixed(1)}%  (${overall.acked}/${resolved})`
  );
  console.log(`未结算 ${overall.rows - resolved} 行（pending / skipped，不进分母）`);
  console.log(`漏响：${overall.missed} 次   发送尝试合计：${overall.fires} 次`);
  console.log(`平均响 ${overall.avg_fires_to_ack ?? "-"} 次才确认`);

  const byWindow = db
    .prepare(
      `SELECT window, label,
              COUNT(*) AS total,
              SUM(CASE WHEN acked_at IS NOT NULL THEN 1 ELSE 0 END) AS acked,
              SUM(CASE WHEN missed IS NOT NULL THEN 1 ELSE 0 END) AS missed,
              ROUND(AVG(CASE WHEN acked_at IS NOT NULL THEN fired END), 2) AS avg_fires
         FROM daily WHERE date >= ?
        GROUP BY window ORDER BY total DESC`
    )
    .all(since);

  console.log("\n按窗口：");
  for (const r of byWindow) {
    const wResolved = r.acked + r.missed;
    const pct = wResolved ? `${((r.acked / wResolved) * 100).toFixed(0)}%` : "—";
    console.log(
      `  ${String(r.label).padEnd(10)} ${String(r.window).padEnd(12)} 确认 ${pct}  (${r.acked}/${wResolved})  漏 ${r.missed}  平均 ${r.avg_fires ?? "-"} 响`
    );
  }

  const recent = db
    .prepare(
      `SELECT date,
              SUM(CASE WHEN acked_at IS NOT NULL THEN 1 ELSE 0 END) AS acked,
              COUNT(*) AS total,
              SUM(fired) AS fires
         FROM daily WHERE date >= ?
        GROUP BY date ORDER BY date DESC LIMIT 7`
    )
    .all(since);

  console.log("\n最近 7 天：");
  for (const r of recent) {
    console.log(`  ${r.date}  确认 ${r.acked}/${r.total}  响 ${r.fires} 次`);
  }
  db.close();
}

function cmdToday() {
  const db = open();
  const a = readAlarms();
  const rows = db.prepare(`SELECT * FROM daily WHERE date = ? ORDER BY window`).all(a.planDate);
  if (!rows.length) {
    console.log(`${a.planDate} 还没同步。先跑：node metrics.mjs sync`);
    db.close();
    return;
  }
  const acked = rows.filter((r) => r.acked_at).length;
  const fires = rows.reduce((s, r) => s + r.fired, 0);
  console.log(`${a.planDate}  窗口 ${rows.length}  确认 ${acked}  响 ${fires} 次`);
  db.close();
}

// ---------- 入口 ----------

const [cmd = "report", arg] = argv;

try {
  switch (cmd) {
    case "init":
      cmdInit();
      break;
    case "sync":
      cmdSync(arg);
      break;
    case "report":
      cmdReport(arg ? Number(arg) : 30);
      break;
    case "today":
      cmdToday();
      break;
    default:
      console.log("用法：node metrics.mjs <init|sync [all|today|past]|report [天数]|today>");
      process.exitCode = 1;
  }
} catch (err) {
  console.error(`metrics 失败：${err instanceof Error ? err.message : String(err)}`);
  process.exitCode = 1;
}
