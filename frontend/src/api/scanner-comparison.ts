import { webClient } from './client'

export type ScreenerSide = 'BUY' | 'SELL'

export interface ScannerComparisonSideSummary {
  screener_side: ScreenerSide
  inhouse_count: number
  chartink_count: number
  intersection_count: number
  intersection: string[]
  jaccard: number | null
  ratio: number | null
  false_positives: string[]
  false_negatives: string[]
  tuning_suggestion: string
}

export interface ChartinkTimelineEvent {
  ts: string
  side: ScreenerSide
  symbols: string[]
  count: number
  posted: boolean
  post_status: string | null
  cycle_id: number
}

export interface InhouseTimelineEvent {
  ts: string
  side: ScreenerSide
  symbols: string[]
  count: number
  posted: boolean
  definition: string
  result_id: number
}

export interface ScannerComparisonTimeline {
  chartink: ChartinkTimelineEvent[]
  inhouse: InhouseTimelineEvent[]
}

export interface ScannerComparisonResponse {
  date: string
  summary: {
    BUY: ScannerComparisonSideSummary
    SELL: ScannerComparisonSideSummary
  }
  timeline: ScannerComparisonTimeline
}

export const scannerComparisonApi = {
  /** Today's in-house vs Chartink comparison (or pass an explicit YYYY-MM-DD). */
  getToday: async (date?: string): Promise<ScannerComparisonResponse> => {
    const params = date ? { date } : undefined
    const response = await webClient.get<{
      status: string
      data: ScannerComparisonResponse
    }>('/chartink/api/scanner-comparison/today', { params })
    return response.data.data
  },
}
