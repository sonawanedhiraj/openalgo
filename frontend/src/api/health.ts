/**
 * Health Monitoring API Client
 * Industry-standard health check endpoints
 */

import { webClient } from './client'

export interface HealthStatus {
  status: 'pass' | 'warn' | 'fail'
  version?: string
  serviceId?: string
  description?: string
}

export interface HealthCheck extends HealthStatus {
  checks?: {
    'database:connectivity'?: Array<{
      componentId: string
      status: 'pass' | 'fail'
      time: string
    }>
    'system:file-descriptors'?: Array<{
      componentId: string
      status: 'pass' | 'warn' | 'fail'
      observedValue: number
      observedUnit: string
      time: string
    }>
    'system:memory'?: Array<{
      componentId: string
      status: 'pass' | 'warn' | 'fail'
      observedValue: number
      observedUnit: string
      time: string
    }>
  }
}

export interface CurrentMetrics {
  timestamp: string
  fd: {
    count: number
    limit: number
    usage_percent: number
    status: 'pass' | 'warn' | 'fail'
  }
  memory: {
    rss_mb: number
    vms_mb: number
    percent: number
    available_mb: number
    swap_mb: number
    status: 'pass' | 'warn' | 'fail'
  }
  database: {
    total: number
    connections: Record<string, number>
    status: 'pass' | 'warn' | 'fail'
  }
  websocket: {
    total: number
    connections: Record<string, { count: number; symbols: number }>
    total_symbols: number
    status: 'pass' | 'warn' | 'fail'
  }
  threads: {
    count: number
    stuck: number
    details?: Array<{ id: number | null; name: string; daemon: boolean; alive: boolean }>
    status: 'pass' | 'warn' | 'fail'
  }
  processes?: Array<{
    pid: number | null
    name: string
    rss_mb: number
    vms_mb: number
    memory_percent: number
  }>
  overall_status: 'pass' | 'warn' | 'fail'
}

export interface HistoricalMetric {
  timestamp: string
  fd_count: number
  memory_rss_mb: number
  db_connections: number
  ws_connections: number
  threads: number
  overall_status: 'pass' | 'warn' | 'fail'
  sys_available_mb?: number | null
  sys_nonpaged_mb?: number | null
}

export interface HealthStats {
  total_samples: number
  time_period_hours: number
  fd: {
    current: number
    avg: number
    min: number
    max: number
    fail_count: number
    warn_count: number
  }
  memory: {
    current_mb: number
    avg_mb: number
    min_mb: number
    max_mb: number
    fail_count: number
    warn_count: number
  }
  database: {
    current: number
    avg: number
    min: number
    max: number
  }
  websocket: {
    current: number
    avg: number
    min: number
    max: number
  }
  threads: {
    current: number
    avg: number
    min: number
    max: number
  }
  status?: {
    overall?: { pass: number; warn: number; fail: number }
    fd?: { warn: number; fail: number }
    memory?: { warn: number; fail: number }
    database?: { warn: number; fail: number }
    websocket?: { warn: number; fail: number }
    threads?: { warn: number; fail: number }
  }
}

export interface HealthAlert {
  id: number
  timestamp: string
  alert_type: string
  severity: 'warn' | 'fail'
  metric_name: string
  metric_value: number
  threshold_value: number
  message: string
  acknowledged: boolean
  resolved: boolean
}

/**
 * Simple health check (for AWS ELB, K8s)
 * No authentication required
 */
export async function getSimpleHealth(): Promise<HealthStatus> {
  const response = await webClient.get<HealthStatus>('/health')
  return response.data
}

/**
 * Detailed health check with DB connectivity
 * No authentication required
 */
export async function getDetailedHealthCheck(): Promise<HealthCheck> {
  const response = await webClient.get<HealthCheck>('/health/check')
  return response.data
}

/**
 * Get current metrics snapshot
 * Requires authentication
 */
export async function getCurrentMetrics(): Promise<CurrentMetrics> {
  const response = await webClient.get<CurrentMetrics>('/health/api/current')
  return response.data
}

/**
 * Get metrics history
 * Requires authentication
 */
export async function getMetricsHistory(hours = 24): Promise<HistoricalMetric[]> {
  const response = await webClient.get<HistoricalMetric[]>('/health/api/history', {
    params: { hours },
  })
  return response.data
}

/**
 * Get aggregated statistics
 * Requires authentication
 */
export async function getHealthStats(hours = 24): Promise<HealthStats> {
  const response = await webClient.get<HealthStats>('/health/api/stats', {
    params: { hours },
  })
  return response.data
}

/**
 * Get active alerts
 * Requires authentication
 */
export async function getActiveAlerts(): Promise<HealthAlert[]> {
  const response = await webClient.get<HealthAlert[]>('/health/api/alerts')
  return response.data
}

/**
 * Acknowledge an alert
 * Requires authentication
 */
export async function acknowledgeAlert(alertId: number): Promise<void> {
  await webClient.post(`/health/api/alerts/${alertId}/acknowledge`)
}

/**
 * Resolve an alert
 * Requires authentication
 */
export async function resolveAlert(alertId: number): Promise<void> {
  await webClient.post(`/health/api/alerts/${alertId}/resolve`)
}

/**
 * Export metrics to CSV
 * Requires authentication
 */
export function exportMetricsCSV(hours = 24): string {
  return `/health/export?hours=${hours}`
}

// ---------------------------------------------------------------------------
// Machine-wide system health (issue #744)
// ---------------------------------------------------------------------------

export type HealthLevel = 'pass' | 'warn' | 'fail'

export interface SystemAppGroup {
  app: string
  class: 'safe' | 'careful' | 'unknown'
  how: string
  rss_mb: number
  processes: number
  suggested?: boolean
  reaches_target?: boolean
}

export interface SystemRecommendations {
  available_mb: number | null
  target_mb: number
  need_mb: number | null
  safe: SystemAppGroup[]
  careful: SystemAppGroup[]
  unknown: SystemAppGroup[]
}

export interface SystemRule {
  label: string
  level: HealthLevel
  raw: HealthLevel
  value: number | null
}

export interface UncleanExitReport {
  detected_at: string
  previous_pid: number | null
  previous_started_at: string | null
  last_sample: {
    at: string
    available_mb: number | null
    nonpaged_mb: number | null
    rss_mb: number | null
  } | null
  min_available_mb: number | null
  min_available_at: string | null
  top_apps: Array<{ app: string; rss_mb: number; class: string }> | null
  crash: {
    time_local?: string | null
    application?: string | null
    module?: string | null
    exception_code?: string | null
    meaning?: string | null
  } | null
}

export interface SystemHealth {
  enabled: boolean
  timestamp?: string
  status?: HealthLevel
  alerts_enabled?: boolean
  readings?: {
    available_mb: number | null
    total_mb: number | null
    nonpaged_mb: number | null
    nonpaged_growth_mb_per_h: number | null
    commit_mb: number | null
    commit_limit_mb: number | null
    commit_pct: number | null
    handles: number | null
    tcp_system: number | null
    tcp_openalgo: number | null
    platform_supported: boolean
  }
  rules?: Record<'free_ram' | 'nonpaged' | 'tcp', SystemRule>
  thresholds?: {
    ram_warn_mb: number
    ram_fail_mb: number
    nonpaged_warn_mb: number
    nonpaged_fail_mb: number
    nonpaged_growth_warn_mb_per_h: number
    tcp_warn: number
    sustain_samples: number
    free_ram_target_mb: number
  }
  recommendations?: SystemRecommendations | null
  unclean_exit: UncleanExitReport | null
}

/**
 * Machine-wide system health. Uses plain fetch, NOT webClient: the navbar
 * polls this from every page, and webClient redirects to /login on a 401.
 * Returns null on any failure so a background poll can never disrupt a page.
 */
export async function getSystemHealth(): Promise<SystemHealth | null> {
  try {
    const response = await fetch('/health/api/system', { credentials: 'same-origin' })
    if (!response.ok) return null
    return (await response.json()) as SystemHealth
  } catch {
    return null
  }
}

/** Hide the unclean-exit banner (user action on /health). */
export async function dismissUncleanExit(): Promise<void> {
  await webClient.post('/health/api/system/unclean_exit/dismiss')
}
