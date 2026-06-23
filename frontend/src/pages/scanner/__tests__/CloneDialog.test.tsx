import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as scanner from '@/api/scanner'
import { render, screen, userEvent } from '@/test/test-utils'
import { CloneDialog } from '../ScannerIndex'

const SOURCE: scanner.ScanDefinitionSummary = {
  id: 1,
  name: 'fno_buy',
  screener_type: 'buy',
  rule_module: null,
  enabled: true,
  created_at: '',
  updated_at: '',
  latest_signals: [],
  today_hit_count: 0,
  parent_definition_id: null,
}

function Wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('CloneDialog', () => {
  beforeEach(() => {
    vi.spyOn(scanner.scannerApi, 'cloneDefinition').mockResolvedValue({
      id: 2,
      name: 'fno_buy_custom',
    })
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows source name in dialog title', () => {
    render(
      <Wrapper>
        <CloneDialog open={true} onOpenChange={vi.fn()} source={SOURCE} />
      </Wrapper>
    )
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText(/fno_buy/)).toBeInTheDocument()
  })

  it('pre-fills name with source_custom', () => {
    render(
      <Wrapper>
        <CloneDialog open={true} onOpenChange={vi.fn()} source={SOURCE} />
      </Wrapper>
    )
    const input = screen.getByLabelText(/new name/i) as HTMLInputElement
    expect(input.value).toBe('fno_buy_custom')
  })

  it('submit button is disabled when name is empty', async () => {
    const user = userEvent.setup()
    render(
      <Wrapper>
        <CloneDialog open={true} onOpenChange={vi.fn()} source={SOURCE} />
      </Wrapper>
    )
    const nameInput = screen.getByLabelText(/new name/i)
    await user.clear(nameInput)
    const submitBtn = screen.getByRole('button', { name: /clone/i })
    expect(submitBtn).toBeDisabled()
  })

  it('calls cloneDefinition on submit with name and null params', async () => {
    const user = userEvent.setup()
    render(
      <Wrapper>
        <CloneDialog open={true} onOpenChange={vi.fn()} source={SOURCE} />
      </Wrapper>
    )
    const submitBtn = screen.getByRole('button', { name: /^clone$/i })
    await user.click(submitBtn)
    expect(scanner.scannerApi.cloneDefinition).toHaveBeenCalledWith(1, {
      name: 'fno_buy_custom',
      parameters_json: null,
    })
  })
})
