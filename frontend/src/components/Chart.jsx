import React, { useEffect, useRef } from 'react'
import * as echarts from 'echarts'

const COLORS = ['#0e3b43', '#2b6cb0', '#f2b705', '#1f7a4d', '#b42318', '#7a5ea8', '#5b6b68']

function option(spec) {
  const unit = spec.unit || ''
  const base = {
    color: COLORS,
    textStyle: { fontFamily: 'Barlow, system-ui, sans-serif', color: '#0e3b43' },
    title: { text: spec.title, left: 0, textStyle: { fontSize: 14, fontWeight: 600, fontFamily: 'Barlow Semi Condensed, Barlow, sans-serif' } },
    tooltip: { trigger: spec.type === 'pie' ? 'item' : 'axis', valueFormatter: (v) => (v == null ? '-' : Number(v).toLocaleString('en-IN', { maximumFractionDigits: 2 })) },
    grid: { left: 8, right: 16, top: 48, bottom: 8, containLabel: true },
  }
  if (spec.type === 'pie') {
    return {
      ...base,
      legend: { orient: 'vertical', right: 0, top: 'middle' },
      series: [{ type: 'pie', radius: ['42%', '70%'], center: ['40%', '55%'], data: spec.x.map((n, i) => ({ name: n, value: spec.series[0].data[i] })),
        label: { formatter: '{b}\n{d}%' } }],
    }
  }
  const right = spec.series.some((s) => s.axis === 'right')
  const marks = (spec.thresholds || []).map((t) => ({ yAxis: t.value, label: { formatter: t.label, position: 'insideEndTop' }, lineStyle: { color: '#b42318', type: 'dashed' } }))
  return {
    ...base,
    legend: spec.series.length > 1 ? { top: 24, left: 0 } : undefined,
    grid: { ...base.grid, top: spec.series.length > 1 ? 64 : 44 },
    xAxis: { type: 'category', data: spec.x, axisLabel: { hideOverlap: true, rotate: spec.x.length > 12 ? 40 : 0 } },
    yAxis: [{ type: 'value', scale: spec.type === 'line', splitLine: { lineStyle: { color: '#e2e8e5' } } }].concat(right ? [{ type: 'value', splitLine: { show: false } }] : []),
    dataZoom: spec.x.length > 60 ? [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 0 }] : undefined,
    series: spec.series.map((s, i) => ({
      name: s.name, type: spec.type === 'line' ? 'line' : 'bar', data: s.data, yAxisIndex: s.axis === 'right' ? 1 : 0,
      smooth: false, showSymbol: spec.x.length <= 40, connectNulls: false, barMaxWidth: 44,
      itemStyle: spec.highlight ? { color: (p) => (spec.highlight.includes(p.name) ? '#f2b705' : COLORS[i]) } : undefined,
      markLine: i === 0 && marks.length ? { symbol: 'none', data: marks } : undefined,
      markArea: i === 0 && spec.band ? { itemStyle: { color: 'rgba(180,35,24,0.08)' }, data: [[{ xAxis: spec.band.from }, { xAxis: spec.band.to }]] } : undefined,
    })),
  }
}

export default function Chart({ spec, height = 330 }) {
  const ref = useRef(null)
  useEffect(() => {
    if (!ref.current || !spec) return
    const c = echarts.init(ref.current, null, { renderer: 'svg' })
    c.setOption(option(spec))
    const ro = new ResizeObserver(() => c.resize())
    ro.observe(ref.current)
    return () => { ro.disconnect(); c.dispose() }
  }, [spec])
  return <div className="chart-box" style={{ height }} ref={ref} role="img" aria-label={spec?.title} />
}
