import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { SystemHealth } from '@/api/health'
import { SystemHealthPanel } from '@/components/health/SystemHealthPanel'
import { SystemHealthDot } from './SystemHealthDot'

vi.mock('lightweight-charts', () => {
  const series = { setData: vi.fn(), createPriceLine: vi.fn() }
  return {
    ColorType: { Solid: 'solid' },
    LineStyle: { Dashed: 2 },
    LineSeries: {},
    createChart: vi.fn(() => ({
      addSeries: vi.fn(() => series),
      applyOptions: vi.fn(),
      remove: vi.fn(),
    })),
  }
})

const HEALTH: SystemHealth = {
  enabled: true,
  timestamp: '2026-09-24T04:10:01+00:00',
  status: 'fail',
  alerts_enabled: false,
  readings: {
    available_mb: 237,
    total_mb: 7549,
    nonpaged_mb: 788,
    nonpaged_growth_mb_per_h: 176,
    commit_mb: 18000,
    commit_limit_mb: 40317,
    commit_pct: 44.6,
    handles: 140000,
    tcp_system: 133,
    tcp_openalgo: 60,
    platform_supported: true,
  },
  rules: {
    free_ram: { label: 'Free RAM', level: 'fail', raw: 'fail', value: 237 },
    nonpaged: { label: 'Non-paged pool', level: 'pass', raw: 'pass', value: 788 },
    tcp: { label: 'System TCP connections', level: 'pass', raw: 'pass', value: 133 },
  },
  thresholds: {
    ram_warn_mb: 400,
    ram_fail_mb: 250,
    nonpaged_warn_mb: 1024,
    nonpaged_fail_mb: 1536,
    nonpaged_growth_warn_mb_per_h: 100,
    tcp_warn: 2000,
    sustain_samples: 3,
    free_ram_target_mb: 1500,
  },
  recommendations: {
    available_mb: 237,
    target_mb: 1500,
    need_mb: 1263,
    safe: [
      {
        app: 'Chrome',
        class: 'safe',
        how: 'Close unused tabs or quit Chrome.',
        rss_mb: 1024,
        processes: 16,
        suggested: true,
        reaches_target: false,
      },
    ],
    careful: [
      {
        app: 'Claude Cowork VM (Hyper-V)',
        class: 'careful',
        how: 'Close Cowork sessions if not needed during market hours.',
        rss_mb: 199,
        processes: 1,
      },
    ],
    unknown: [],
  },
  unclean_exit: {
    detected_at: '2026-09-24T06:49:00+00:00',
    previous_pid: 27080,
    previous_started_at: '2026-09-24T03:01:00+00:00',
    last_sample: {
      at: '2026-09-24T04:11:07+00:00',
      available_mb: 620,
      nonpaged_mb: null,
      rss_mb: 921,
    },
    min_available_mb: 236.8,
    min_available_at: '2026-09-24T04:10:01+00:00',
    top_apps: [{ app: 'Claude app', rss_mb: 1600, class: 'careful' }],
    crash: {
      application: 'python.exe',
      module: 'KERNELBASE.dll',
      exception_code: '0x40000015',
      meaning: 'abort() — e.g. a failed libzmq assertion (the 10055 crash)',
    },
  },
}

function mockFetch(response: { ok: boolean; body?: unknown }) {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: response.ok, json: async () => response.body }))
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('SystemHealthDot', () => {
  it('shows a critical chip that links to /health', async () => {
    mockFetch({ ok: true, body: HEALTH })
    render(
      <MemoryRouter>
        <SystemHealthDot />
      </MemoryRouter>
    )
    const link = await screen.findByTestId('system-health-dot')
    expect(link).toHaveAttribute('href', '/health')
    expect(link.getAttribute('title')).toContain('System: critical')
    expect(link.getAttribute('title')).toContain('free RAM 237 MB')
    expect(link.getAttribute('title')).toContain('clean shutdown')
  })

  it('renders nothing when the endpoint rejects (never redirects)', async () => {
    mockFetch({ ok: false })
    const { container } = render(
      <MemoryRouter>
        <SystemHealthDot />
      </MemoryRouter>
    )
    await waitFor(() => expect(fetch).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })
})

describe('SystemHealthPanel', () => {
  it('shows the unclean-exit banner, system readings and free-up-memory advice', () => {
    render(<SystemHealthPanel health={HEALTH} history={[]} onDismissUncleanExit={() => {}} />)
    const banner = screen.getByTestId('unclean-exit-banner')
    expect(banner).toHaveTextContent('0x40000015')
    expect(banner).toHaveTextContent('Lowest free RAM in its final 5 minutes: 237 MB at 09:40:01')
    const system = screen.getByTestId('system-card')
    expect(system).toHaveTextContent('Critical')
    expect(system).toHaveTextContent('Telegram alerts off (calibrating)')
    expect(system).toHaveTextContent('788 MB')
    const advice = screen.getByTestId('free-up-memory-card')
    expect(advice).toHaveTextContent('Chrome')
    expect(advice).toHaveTextContent('Claude Cowork VM (Hyper-V)')
    expect(advice).toHaveTextContent('OpenAlgo never closes apps itself')
  })

  it('renders nothing when system health is disabled', () => {
    const { container } = render(
      <SystemHealthPanel
        health={{ enabled: false, unclean_exit: null }}
        history={[]}
        onDismissUncleanExit={() => {}}
      />
    )
    expect(container).toBeEmptyDOMElement()
  })
})
