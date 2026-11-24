/*
This file is part of owo-dusk.

Copyright (c) 2024-present EchoQuill

Portions of this file are based on code by EchoQuill, licensed under the
GNU General Public License v3.0 (GPL-3.0).

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
*/

var password = localStorage.getItem("password") || prompt("Enter Password");

if (password) {
    localStorage.setItem("password", password);
} else {
    alert("Password is required.");
    location.reload();
}

const REQUEST_TIMEOUT_MS = 180000;
const CONSOLE_INTERVAL_MS = 2000;
const HOUR_LABELS = [
    "12 AM", "1 AM", "2 AM", "3 AM", "4 AM", "5 AM",
    "6 AM", "7 AM", "8 AM", "9 AM", "10 AM", "11 AM",
    "12 PM", "1 PM", "2 PM", "3 PM", "4 PM", "5 PM",
    "6 PM", "7 PM", "8 PM", "9 PM", "10 PM", "11 PM",
];
const WEEKDAY_LABELS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const WEEKDAY_COLORS = [
    "rgb(190, 128, 248)",
    "rgb(170, 88, 247)",
    "rgb(130, 45, 209)",
    "rgb(121, 28, 209)",
    "rgb(102, 10, 189)",
    "rgb(78, 4, 146)",
    "rgb(53, 3, 100)",
];

var chartInstances = {};
var inFlight = {};
var authFailed = false;

// Each section refreshes on its own loop, so one slow or failing request only affects its own section.
const sections = {
    commands: { chartId: "total_commands", load: loadCommands },
    cowoncy: { chartId: "cowoncy_earnings", load: loadCowoncy },
    weekly: { chartId: "weekly_runtimes", load: loadWeeklyRuntime },
    gamble: { chartId: "gamble_earnings", load: loadGambleWinrates },
};

document.addEventListener("DOMContentLoaded", () => {
    const refreshMs = (window.REFRESH_INTERVAL_SECONDS || 15) * 1000;
    for (const key of Object.keys(sections)) {
        runSectionLoop(key, refreshMs);
    }
    pollConsole();
});

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

async function runSectionLoop(key, intervalMs) {
    while (true) {
        await refreshSection(key);
        await sleep(intervalMs);
    }
}

async function pollConsole() {
    while (true) {
        try {
            logMessage(await fetchDashboard("/api/console", false));
        } catch (error) {
            console.warn("Console poll failed:", error.message);
        }
        await sleep(CONSOLE_INTERVAL_MS);
    }
}

async function refreshSection(key) {
    // One request per section at a time. The reset button goes through here too.
    if (inFlight[key]) return;
    inFlight[key] = true;
    const section = sections[key];
    try {
        await section.load();
    } catch (error) {
        console.warn(`${key} update failed:`, error.message);
        setChartStatus(section.chartId, "Failed to load data, retrying...");
    } finally {
        inFlight[key] = false;
    }
}

async function resetChart(chartId) {
    const key = Object.keys(sections).find((k) => sections[k].chartId === chartId);
    if (key) await refreshSection(key);
}

async function fetchDashboard(path, expectJson = true) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
        const response = await fetch(path, {
            method: "GET",
            headers: { password: password },
            signal: controller.signal,
        });
        if (response.status === 401) {
            handleWrongPassword();
            throw new Error("wrong password");
        }
        if (!response.ok) {
            throw new Error(`HTTP ${response.status} from ${path}`);
        }
        if (!expectJson) {
            return await response.text();
        }
        const data = await response.json();
        if (data.status !== "success") {
            throw new Error(data.message || `${path} returned an error`);
        }
        return data;
    } finally {
        clearTimeout(timer);
    }
}

function handleWrongPassword() {
    if (authFailed) return;
    authFailed = true;
    localStorage.removeItem("password");
    alert("Wrong password.");
    location.reload();
}

function logMessage(text) {
    if (typeof text !== "string") return;
    const consoleElement = document.getElementById("messages");
    consoleElement.innerHTML = text;
    consoleElement.scrollTop = consoleElement.scrollHeight;
}

function setCard(cardId, value) {
    if (value === undefined || value === null || Number.isNaN(value)) {
        console.warn(`${cardId}: no value in response`);
        return;
    }
    document.getElementById(cardId).innerText = value;
}

function setChartStatus(chartId, message) {
    const element = document.getElementById(`status_${chartId}`);
    if (element) element.innerText = message;
}

function drawChart(chartId, type, options, data, hasData) {
    if (typeof Chart === "undefined") {
        setChartStatus(chartId, "Chart library could not be loaded.");
        return;
    }
    try {
        const existing = chartInstances[chartId];
        if (existing) {
            existing.data.labels = data.labels;
            existing.data.datasets = data.datasets;
            existing.update();
        } else {
            chartInstances[chartId] = new Chart(document.getElementById(chartId), { type, data, options });
        }
        setChartStatus(chartId, hasData ? "" : "No data yet.");
    } catch (error) {
        console.warn(`${chartId} could not be drawn:`, error.message);
        setChartStatus(chartId, "Chart could not be drawn.");
    }
}

async function loadCommands() {
    const data = await fetchDashboard("/api/fetch_cmd_data");
    setCard("total_commands_card", data.count.reduce((sum, n) => sum + n, 0));
    drawChart("total_commands", "doughnut", doughnutOptions(), {
        labels: [...data.command_names],
        datasets: [
            {
                label: "Times sent",
                data: data.count,
                backgroundColor: data.count.map((_, i) => paletteColor(i)),
                hoverOffset: 4,
            },
        ],
    }, data.count.length > 0);
}

async function loadCowoncy() {
    const data = await fetchDashboard("/api/fetch_cowoncy_data");
    setCard("total_cash_card", data.total_cash);
    setCard("total_captchas_card", data.total_captchas);
    drawChart("cowoncy_earnings", "line", cowoncyOptions(), data.data, data.data.datasets.length > 0);
}

async function loadWeeklyRuntime() {
    const data = await fetchDashboard("/api/fetch_weekly_runtime");
    setCard("total_uptime_card", uptime_calc(data.current_uptime));
    drawChart("weekly_runtimes", "bar", weeklyOptions(), {
        labels: [...(data.runtime_labels || WEEKDAY_LABELS)],
        datasets: [
            {
                label: "Hours Ran",
                data: data.runtime_data,
                backgroundColor: [...WEEKDAY_COLORS],
                hoverOffset: 4,
            },
        ],
    }, data.runtime_data.some((minutes) => minutes > 0));
}

async function loadGambleWinrates() {
    const data = await fetchDashboard("/api/fetch_gamble_data");
    drawChart("gamble_earnings", "line", gambleOptions(), {
        labels: [...HOUR_LABELS],
        datasets: [
            {
                label: "Winnings",
                data: data.win_data,
                fill: false,
                borderColor: "rgb(0, 200, 0)",
                backgroundColor: "rgb(0, 200, 0)",
                tension: 0.2,
            },
            {
                label: "Losses",
                data: data.lose_data,
                borderColor: "rgb(220, 53, 69)",
                backgroundColor: "rgb(220, 53, 69)",
                tension: 0.2,
            },
        ],
    }, data.win_data.some((n) => n > 0) || data.lose_data.some((n) => n > 0));
}

function paletteColor(index) {
    // Fixed colours, so a refresh does not reshuffle the doughnut.
    return `hsl(${(index * 137) % 360}, 70%, 60%)`;
}

function gridStyle() {
    return { display: true, color: "rgba(102, 64, 199, 0.3)" };
}

function legendStyle(position) {
    return { position, labels: { boxWidth: 10, font: { size: 11 } } };
}

function zoomStyle() {
    return {
        pan: { enabled: true, mode: "xy" },
        zoom: { wheel: { enabled: true }, pinch: { enabled: true }, drag: { enabled: true }, mode: "xy" },
    };
}

function doughnutOptions() {
    return {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: { legend: legendStyle("left") },
    };
}

function gambleOptions() {
    return {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: { legend: legendStyle("top"), zoom: zoomStyle() },
        scales: {
            y: {
                beginAtZero: true,
                title: { display: true, text: "Win/Loss count" },
                grid: gridStyle(),
            },
            x: {
                title: { display: true, text: "Time of Day" },
                grid: gridStyle(),
            },
        },
    };
}

function weeklyOptions() {
    return {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: { legend: legendStyle("top"), zoom: zoomStyle() },
        scales: {
            x: { grid: gridStyle() },
            y: { beginAtZero: true, grid: gridStyle() },
        },
    };
}

function cowoncyOptions() {
    return {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "nearest", intersect: false },
        plugins: {
            legend: legendStyle("top"),
            tooltip: { mode: "nearest", intersect: false },
            zoom: {
                limits: { y: { min: 0, max: "original" } },
                pan: { enabled: true, mode: "xy" },
                zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: "xy" },
            },
        },
        scales: {
            x: {
                type: "category",
                ticks: { maxRotation: 45, minRotation: 30 },
                grid: gridStyle(),
            },
            y: { beginAtZero: true, grid: gridStyle() },
        },
    };
}

function uptime_calc(totalSeconds) {
    let sec = Math.max(0, Math.round(Number(totalSeconds) || 0));

    let hours = Math.floor(sec / 3600);
    let minutes = Math.floor((sec % 3600) / 60);
    let seconds = Math.round(sec % 60);

    if (seconds === 60) {
        seconds = 0;
        if (minutes === 59) {
            minutes = 0;
            hours += 1;
        } else {
            minutes += 1;
        }
    }

    const h = String(hours).padStart(2, "0");
    const m = String(minutes).padStart(2, "0");
    const s = String(seconds).padStart(2, "0");

    return `${h}:${m}:${s}`;
}
