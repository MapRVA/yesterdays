// Entry point for the statistics page (templates/stats.html): a cumulative
// georeferences line chart and an image-status pie chart, fed by JSON lists
// the view writes into the hidden #chart-data element's data attributes.
import Chart, { type TooltipItem } from "chart.js/auto";
import "chartjs-adapter-date-fns";

import {
  dangerColor,
  primaryColor,
  secondaryColor,
  successColor,
  warningColor,
} from "../components/map_display/colors";

// ISO date strings, parsed by the date-fns adapter on the time axis.
type DailyPoint = { x: string; y: number };

function readJsonData<T>(el: HTMLElement, key: string): T[] {
  return JSON.parse(el.dataset[key] || "[]") as T[];
}

function getCanvas(id: string): HTMLCanvasElement | null {
  const canvas = document.getElementById(id);
  if (!(canvas instanceof HTMLCanvasElement)) {
    console.error(`Chart canvas #${id} not found`);
    return null;
  }
  return canvas;
}

function renderCumulativeChart(
  canvas: HTMLCanvasElement,
  labels: string[],
  counts: number[],
): void {
  const dataPoints: DailyPoint[] = labels.map((label, index) => ({
    x: label,
    y: counts[index] ?? 0,
  }));

  new Chart<"line", DailyPoint[]>(canvas, {
    type: "line",
    data: {
      datasets: [
        {
          label: "Cumulative Georeferences",
          data: dataPoints,
          borderColor: primaryColor,
          backgroundColor: `color-mix(in srgb, ${primaryColor} 20%, transparent)`,
          fill: true,
          tension: 0.1,
          pointRadius: 0,
          pointHoverRadius: 4,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      // Points are hidden (pointRadius: 0), so don't require the cursor to
      // hit one: show the day nearest the cursor horizontally.
      interaction: { mode: "nearest", axis: "x", intersect: false },
      scales: {
        x: {
          type: "time",
          time: {
            unit: "day",
            displayFormats: {
              day: "MMM d, yyyy",
            },
            tooltipFormat: "PPP",
          },
          title: {
            display: true,
            text: "Date",
          },
        },
        y: {
          beginAtZero: true,
          title: {
            display: true,
            text: "Cumulative Georeferences",
          },
        },
      },
    },
  });
}

function renderStatusChart(
  canvas: HTMLCanvasElement,
  labels: string[],
  counts: number[],
): void {
  // Not georeferenced, low, medium, high confidence — the view's label order.
  const colors = [secondaryColor, dangerColor, warningColor, successColor];

  new Chart(canvas, {
    type: "pie",
    data: {
      labels,
      datasets: [
        {
          label: "Image Status",
          data: counts,
          backgroundColor: colors.map(
            (color) => `color-mix(in srgb, ${color} 70%, transparent)`,
          ),
          borderColor: colors,
          borderWidth: 1,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        tooltip: {
          callbacks: {
            label: (context: TooltipItem<"pie">) => {
              const value = context.parsed || 0;
              const imageWord = value === 1 ? "Image" : "Images";
              return ` ${value} ${imageWord}`;
            },
          },
        },
      },
    },
  });
}

document.addEventListener("DOMContentLoaded", () => {
  const chartDataEl = document.getElementById("chart-data");
  if (!chartDataEl) {
    console.error("Chart data element not found");
    return;
  }

  const dailyCanvas = getCanvas("dailyGeoreferencesChart");
  if (dailyCanvas) {
    renderCumulativeChart(
      dailyCanvas,
      readJsonData<string>(chartDataEl, "dailyLabels"),
      readJsonData<number>(chartDataEl, "dailyCounts"),
    );
  }

  const statusCanvas = getCanvas("imageStatusChart");
  if (statusCanvas) {
    renderStatusChart(
      statusCanvas,
      readJsonData<string>(chartDataEl, "statusLabels"),
      readJsonData<number>(chartDataEl, "statusCounts"),
    );
  }
});
