#!/usr/bin/env node
/* L.54 — the Calendars panel's pure helpers (web/public/calendar.js).
 *
 * Loads the real calendar.js into a bare `window` with vm (no DOM, no npm install) and
 * calls JarvisCalendar._pure, the same way verify_daemons_panel.js does for daemons.js.
 *
 * Pins: how each view's window is worked out (Monday-first weeks, a 42-day month grid, a
 * 14-day agenda); which days an event touches (an all-day event ends at the NEXT midnight
 * and must not spill onto it); the order a day's events show in; what "Remind me" would
 * create or why it can't (a reminder is only ever a NEW one-shot job: nothing here edits
 * one, because the scheduler has no operation that does); what the event form sends; and
 * that the link guide has the providers and steps it says it has.
 *
 * Run: node tests/verify_calendar_panel.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const file = path.join(__dirname, "..", "web", "public", "calendar.js");
const source = fs.readFileSync(file, "utf8");

const win = { localStorage: { getItem: () => null, setItem: () => {} } };
win.window = win;
const sandbox = vm.createContext({ window: win, document: {}, console, Date, Set, Math, Number, String, Object, Array, Promise, setTimeout, encodeURIComponent });
vm.runInContext(source, sandbox, { filename: "calendar.js" });

const C = win.JarvisCalendar && win.JarvisCalendar._pure;
let pass = 0;
let fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log("ok       " + name); }
  else { fail++; console.log("FAILED   " + name + (detail !== undefined ? ": " + JSON.stringify(detail) : "")); }
}

check("calendar.js exposes JarvisCalendar with open/close/isOpen", Boolean(win.JarvisCalendar) && typeof win.JarvisCalendar.open === "function" && typeof win.JarvisCalendar.close === "function" && typeof win.JarvisCalendar.isOpen === "function");
check("and its pure helpers", Boolean(C));
if (!C) { console.log("\n0 passed, 1 failed"); process.exit(1); }

const d = (y, m, day, h = 0, mi = 0, s = 0) => new Date(y, m - 1, day, h, mi, s);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ---- dates -----------------------------------------------------------------------------
{
  const dt = C.parseLocal("2026-10-12T14:05:09");
  check("parseLocal reads a local timestamp", dt.getFullYear() === 2026 && dt.getMonth() === 9 && dt.getDate() === 12 && dt.getHours() === 14 && dt.getMinutes() === 5 && dt.getSeconds() === 9);
  check("parseLocal reads a bare date as midnight", C.parseLocal("2026-10-12").getHours() === 0);
  check("parseLocal reads HH:MM without seconds", C.parseLocal("2026-10-12T09:30").getMinutes() === 30);
  check("parseLocal gives null for rubbish", C.parseLocal("soon") === null && C.parseLocal("") === null && C.parseLocal(null) === null);
  check("toLocalIso round-trips", C.toLocalIso(C.parseLocal("2026-10-12T14:05:09")) === "2026-10-12T14:05:09");
  check("ymd pads", C.ymd(d(2026, 1, 5)) === "2026-01-05");
  check("hhmm pads", C.hhmm(d(2026, 1, 5, 7, 3)) === "07:03");
  check("addDays crosses a month and keeps the clock time", C.toLocalIso(C.addDays(d(2026, 10, 30, 8, 15), 3)) === "2026-11-02T08:15:00");
  check("addDays goes backwards", C.ymd(C.addDays(d(2026, 3, 1), -1)) === "2026-02-28");
  // A clock change must not shift local midnight (adding 24h of milliseconds would).
  check("addDays keeps midnight across late October and late March", C.addDays(d(2026, 10, 24), 3).getHours() === 0 && C.addDays(d(2026, 3, 27), 3).getHours() === 0);
}

// ---- weeks and windows ----------------------------------------------------------------------
{
  check("a Wednesday's week starts on the Monday", C.ymd(C.startOfWeek(d(2026, 10, 14))) === "2026-10-12");
  check("a Monday starts its own week", C.ymd(C.startOfWeek(d(2026, 10, 12))) === "2026-10-12");
  check("a Sunday belongs to the week that began the Monday before", C.ymd(C.startOfWeek(d(2026, 10, 18))) === "2026-10-12");
  const week = C.windowFor("week", d(2026, 10, 14, 15));
  check("the week window is Monday to the next Monday", C.ymd(week.start) === "2026-10-12" && C.ymd(week.end) === "2026-10-19");
  const agenda = C.windowFor("agenda", d(2026, 10, 14, 15));
  check("the agenda window is 14 days from the start of the day", C.ymd(agenda.start) === "2026-10-14" && C.ymd(agenda.end) === "2026-10-28" && agenda.start.getHours() === 0);
  const month = C.windowFor("month", d(2026, 10, 14));
  check("the month window is six whole weeks from the Monday on or before the 1st", C.ymd(month.start) === "2026-09-28" && C.ymd(month.end) === "2026-11-09");
  check("the request never exceeds the server's 400-day limit", (month.end - month.start) / 86400000 <= 400);
  check("stepping a week moves seven days", C.ymd(C.stepAnchor("week", d(2026, 10, 14), 1)) === "2026-10-21" && C.ymd(C.stepAnchor("week", d(2026, 10, 14), -1)) === "2026-10-07");
  check("stepping a month lands on the 1st of the next / previous month", C.ymd(C.stepAnchor("month", d(2026, 10, 31), 1)) === "2026-11-01" && C.ymd(C.stepAnchor("month", d(2026, 1, 15), -1)) === "2025-12-01");
  check("stepping the agenda moves 14 days", C.ymd(C.stepAnchor("agenda", d(2026, 10, 14), 1)) === "2026-10-28");
  check("the month label names the month and year", C.rangeLabel("month", d(2026, 10, 14)) === "October 2026");
  check("the week label gives both ends", C.rangeLabel("week", d(2026, 10, 14)) === "12 Oct – 18 Oct 2026", C.rangeLabel("week", d(2026, 10, 14)));
  const grid = C.monthGrid(d(2026, 10, 14));
  check("the month grid has 42 cells", grid.length === 42);
  check("it starts on a Monday", grid[0].date.getDay() === 1 && grid[0].key === "2026-09-28");
  check("exactly the 31 days of October are in the month", grid.filter((c) => c.inMonth).length === 31 && grid.filter((c) => c.inMonth).every((c) => c.key.startsWith("2026-10")));
  check("February in a leap-free year has 28 in-month cells", C.monthGrid(d(2027, 2, 10)).filter((c) => c.inMonth).length === 28);
  check("cells are consecutive days", grid.every((c, i) => i === 0 || (c.date - grid[i - 1].date) >= 82800000));
}

// ---- which days an event touches ---------------------------------------------------------------
{
  const timed = { start: "2026-10-12T14:00:00", end: "2026-10-12T15:00:00", all_day: false };
  check("a timed event touches its own day", same(C.eventDayKeys(timed), ["2026-10-12"]));
  const allday = { start: "2026-10-12T00:00:00", end: "2026-10-13T00:00:00", all_day: true };
  check("an all-day event does NOT spill onto the next midnight", same(C.eventDayKeys(allday), ["2026-10-12"]), C.eventDayKeys(allday));
  const trip = { start: "2026-10-12T00:00:00", end: "2026-10-15T00:00:00", all_day: true };
  check("a three-day all-day event touches three days", same(C.eventDayKeys(trip), ["2026-10-12", "2026-10-13", "2026-10-14"]));
  const overnight = { start: "2026-10-12T22:00:00", end: "2026-10-13T02:00:00", all_day: false };
  check("an overnight event touches both days", same(C.eventDayKeys(overnight), ["2026-10-12", "2026-10-13"]));
  const endsAtMidnight = { start: "2026-10-12T22:00:00", end: "2026-10-13T00:00:00", all_day: false };
  check("an event that ends exactly at midnight stays on its day", same(C.eventDayKeys(endsAtMidnight), ["2026-10-12"]));
  check("an event with a bad start touches nothing", same(C.eventDayKeys({ start: "x", end: "y" }), []));
  check("an event with no end is treated as a point", same(C.eventDayKeys({ start: "2026-10-12T09:00:00" }), ["2026-10-12"]));
}

// ---- grouping and order -------------------------------------------------------------------------------
{
  const evs = [
    { id: "c", title: "Zeta", start: "2026-10-12T09:00:00", end: "2026-10-12T10:00:00", all_day: false },
    { id: "a", title: "Holiday", start: "2026-10-12T00:00:00", end: "2026-10-13T00:00:00", all_day: true },
    { id: "b", title: "Alpha", start: "2026-10-12T09:00:00", end: "2026-10-12T09:30:00", all_day: false },
    { id: "d", title: "Early", start: "2026-10-12T07:00:00", end: "2026-10-12T08:00:00", all_day: false },
    { id: "e", title: "Next day", start: "2026-10-13T09:00:00", end: "2026-10-13T10:00:00", all_day: false },
  ];
  const groups = C.groupByDay(evs);
  check("events land on their days", Object.keys(groups).sort().join() === "2026-10-12,2026-10-13");
  check("all-day first, then by time, then by title", groups["2026-10-12"].map((e) => e.id).join("") === "adbc", groups["2026-10-12"].map((e) => e.id).join(""));
  check("grouping does not mutate the input order", evs.map((e) => e.id).join("") === "cabde");
  check("an empty list groups to nothing", Object.keys(C.groupByDay([])).length === 0 && Object.keys(C.groupByDay(null)).length === 0);
}

// ---- labels ---------------------------------------------------------------------------------------------------------
{
  check("a same-day range", C.timeLabel({ start: "2026-10-12T14:00:00", end: "2026-10-12T15:30:00", all_day: false }) === "14:00–15:30");
  check("an all-day label", C.timeLabel({ all_day: true, start: "2026-10-12T00:00:00", end: "2026-10-13T00:00:00" }) === "All day");
  check("an overnight range names the end day", /^22:00 → \w{3} 02:00$/.test(C.timeLabel({ start: "2026-10-12T22:00:00", end: "2026-10-13T02:00:00", all_day: false })), C.timeLabel({ start: "2026-10-12T22:00:00", end: "2026-10-13T02:00:00", all_day: false }));
  check("a point in time shows just the start", C.timeLabel({ start: "2026-10-12T14:00:00", end: "2026-10-12T14:00:00", all_day: false }) === "14:00");
  check("whenLabel for a timed event", C.whenLabel({ start: "2026-10-12T14:00:00", end: "2026-10-12T15:00:00", all_day: false }) === "Mon 12 Oct 2026 · 14:00–15:00", C.whenLabel({ start: "2026-10-12T14:00:00", end: "2026-10-12T15:00:00", all_day: false }));
  check("whenLabel for a one-day all-day event", C.whenLabel({ start: "2026-10-12T00:00:00", end: "2026-10-13T00:00:00", all_day: true }) === "Mon 12 Oct 2026 · all day");
  check("whenLabel for a multi-day all-day event shows the last day, not the day after", /→ Wed 14 Oct · all day$/.test(C.whenLabel({ start: "2026-10-12T00:00:00", end: "2026-10-15T00:00:00", all_day: true })), C.whenLabel({ start: "2026-10-12T00:00:00", end: "2026-10-15T00:00:00", all_day: true }));
  const now = d(2026, 10, 12, 10);
  check("dayLabel says Today / Tomorrow / Yesterday", C.dayLabel(d(2026, 10, 12), now).startsWith("Today") && C.dayLabel(d(2026, 10, 13), now).startsWith("Tomorrow") && C.dayLabel(d(2026, 10, 11), now).startsWith("Yesterday"));
  check("a later day is just the date", C.dayLabel(d(2026, 10, 20), now) === "Tue 20 Oct");
  check("lead labels read naturally", C.leadLabel(5) === "5 minutes before" && C.leadLabel(60) === "1 hour before" && C.leadLabel(120) === "2 hours before" && C.leadLabel(1440) === "1 day before");
  check("agoLabel", C.agoLabel("2026-10-12T09:59:50", now) === "just now" && C.agoLabel("2026-10-12T09:30:00", now) === "30 min ago" && C.agoLabel("2026-10-12T07:00:00", now) === "3 h ago" && C.agoLabel("2026-10-10T10:00:00", now) === "2 d ago" && C.agoLabel(null, now) === "never");
}

// ---- Remind me: only ever a NEW one-shot reminder -------------------------------------------------------------------------
{
  const now = d(2026, 10, 12, 10, 0, 0);
  const meeting = { id: "x", title: "Design review", location: "Room 4", start: "2026-10-12T14:00:00", end: "2026-10-12T15:00:00", all_day: false };
  const plan = C.reminderPlan(meeting, 30, now);
  check("a reminder 30 minutes before is planned", plan.ok === true, plan);
  check("it fires at start minus the lead, as a local timestamp the scheduler reads", plan.when === "2026-10-12T13:30:00", plan.when);
  check("the message names the event, the time, the place and the lead", plan.message === "Reminder: Design review at 14:00 — Room 4 (in 30 minutes)", plan.message);
  check("a day before works", C.reminderPlan(Object.assign({}, meeting, { start: "2026-10-14T14:00:00", end: "2026-10-14T15:00:00" }), 1440, now).when === "2026-10-13T14:00:00");
  check("an hour before reads 1 hour", /\(in 1 hour\)$/.test(C.reminderPlan(meeting, 60, now).message));
  const late = C.reminderPlan(meeting, 240, now);
  check("a reminder time already in the past is refused with a reason", late.ok === false && /already passed/.test(late.error), late);
  check("one a few seconds away is refused too (the scheduler could not fire it)", C.reminderPlan(Object.assign({}, meeting, { start: "2026-10-12T10:10:00" }), 10, now).ok === false);
  check("a nonsense lead is refused", C.reminderPlan(meeting, "soon", now).ok === false && C.reminderPlan(meeting, -5, now).ok === false && C.reminderPlan(meeting, 999999, now).ok === false);
  check("an event with no usable start is refused", C.reminderPlan({ title: "x", start: "nope" }, 30, now).ok === false && C.reminderPlan(null, 30, now).ok === false);
  const allday = { id: "y", title: "Mum's birthday", location: "", start: "2026-10-13T00:00:00", end: "2026-10-14T00:00:00", all_day: true };
  const ad = C.reminderPlan(allday, 30, now);
  check("an all-day event reminds at 09:00 that morning, whatever the lead", ad.ok && ad.when === "2026-10-13T09:00:00", ad);
  check("and says so", ad.message === "Reminder: Mum's birthday today (all day)", ad.message);
  const adToday = C.reminderPlan(Object.assign({}, allday, { start: "2026-10-12T00:00:00", end: "2026-10-13T00:00:00" }), 30, d(2026, 10, 12, 11));
  check("an all-day event whose 09:00 has gone is refused", adToday.ok === false && /09:00/.test(adToday.error), adToday);
  check("the plan has no job id and no update field: it can only create", !("id" in plan) && !("job" in plan) && !("update" in plan));
  check("an overlong title is cut so the message stays small", C.reminderPlan(Object.assign({}, meeting, { title: "x".repeat(900) }), 30, now).message.length <= 300);
}

// ---- the event form ---------------------------------------------------------------------------------------------------------------
{
  const good = { title: "  Dentist ", date: "2026-10-14", all_day: false, time: "15:30", duration: "45", location: " Main St ", description: " forms " };
  const out = C.eventPayload(good);
  check("a good form is accepted", out.ok === true, out);
  check("it is trimmed and shaped for the server", same(out.event, { title: "Dentist", date: "2026-10-14", all_day: false, location: "Main St", description: "forms", time: "15:30", duration_minutes: 45 }), out.event);
  const allday = C.eventPayload({ title: "Trip", date: "2026-10-14", all_day: true, time: "", duration: "" });
  check("an all-day event sends no time or duration", allday.ok && !("time" in allday.event) && !("duration_minutes" in allday.event), allday);
  check("a title is required", C.eventPayload(Object.assign({}, good, { title: "   " })).ok === false);
  check("a date is required and must be YYYY-MM-DD", C.eventPayload(Object.assign({}, good, { date: "" })).ok === false && C.eventPayload(Object.assign({}, good, { date: "12/10/2026" })).ok === false);
  check("a start time is required unless all-day", C.eventPayload(Object.assign({}, good, { time: "" })).ok === false);
  check("a duration under five minutes is refused", C.eventPayload(Object.assign({}, good, { duration: "1" })).ok === false);
  check("a duration over a day is refused", C.eventPayload(Object.assign({}, good, { duration: "2000" })).ok === false);
  check("a non-number duration is refused", C.eventPayload(Object.assign({}, good, { duration: "lots" })).ok === false);
  const back = C.formFromEvent({ title: "Dentist", start: "2026-10-14T15:30:00", end: "2026-10-14T16:15:00", all_day: false, location: "Main St", description: "forms" });
  check("an event becomes its form", back.title === "Dentist" && back.date === "2026-10-14" && back.time === "15:30" && back.duration === 45 && back.all_day === false, back);
  check("an all-day event becomes an all-day form with a usable default time", C.formFromEvent({ title: "T", start: "2026-10-14T00:00:00", end: "2026-10-15T00:00:00", all_day: true }).all_day === true && C.formFromEvent({ title: "T", start: "2026-10-14T00:00:00", end: "2026-10-15T00:00:00", all_day: true }).time === "09:00");
  const roundTrip = C.eventPayload(C.formFromEvent({ title: "Dentist", start: "2026-10-14T15:30:00", end: "2026-10-14T16:15:00", all_day: false }));
  check("editing without changing anything sends the same thing back", roundTrip.ok && roundTrip.event.time === "15:30" && roundTrip.event.duration_minutes === 45, roundTrip);
}

// ---- sources and the guide -------------------------------------------------------------------------------------------------------------
{
  const classes = ["Work", "Home", "School", "Family", "x", "", null].map((n) => C.sourceClass(n));
  check("a linked calendar never gets Jarvis's own colour", classes.every((c) => /^cal-src-[1-5]$/.test(c)), classes);
  check("a calendar keeps its colour", C.sourceClass("Work") === C.sourceClass("Work"));
  check("five providers", C.PROVIDERS.length === 5 && C.PROVIDERS.map((p) => p.id).join() === "google,outlook,apple,fastmail,other");
  check("each has a label, a hint, a default name and exactly three steps", C.PROVIDERS.every((p) => p.label && p.hint && p.defaultName && Array.isArray(p.steps) && p.steps.length === 3 && p.steps.every((s) => typeof s === "string" && s.length > 10)));
  check("the Google steps point at the secret iCal address", /Secret address in iCal format/.test(C.PROVIDERS[0].steps.join(" ")));
  check("the Apple steps say webcal:// works as it is", /webcal:\/\//.test(C.PROVIDERS[2].steps.join(" ")));
  check("the lead choices are sorted and start at five minutes", C.LEAD_CHOICES.join() === "5,10,15,30,60,120,1440");
  check("the agenda is two weeks", C.AGENDA_DAYS === 14);
}

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
