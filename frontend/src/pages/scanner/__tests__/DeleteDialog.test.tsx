import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as scanner from '@/api/scanner'
import { render, screen, userEvent } from '@/test/test-utils'
import { DeleteDialog } from '../ScannerIndex'

const CLONE_DEF: scanner.ScanDefinitionSummary = {
  id: 2,
  name: 'fno_buy_custom',
  screener_type: 'buy',
  rule_module: null,
  enabled: true,
  created_at: '',
  updated_at: '',
  latest_signals: [],
  today_hit_count: 0,
  parent_definition_id: 1,
}

function Wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
}

describe('DeleteDialog', () => {
  beforeEach(() => {
    vi.spyOn(scanner.scannerApi, 'deleteDefinition').mockResolvedValue({ id: 2 })
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('shows definition name in dialog', () => {
    render(
      <Wrapper>
        <DeleteDialog open={true} onOpenChange={vi.fn()} definition={CLONE_DEF} />
      </Wrapper>
    )
    expect(screen.getByText(/fno_buy_custom/)).toBeInTheDocument()
  })

  it('calls deleteDefinition on confirm', async () => {
    const user = userEvent.setup()
    render(
      <Wrapper>
        <DeleteDialog open={true} onOpenChange={vi.fn()} definition={CLONE_DEF} />
      </Wrapper>
    )
    const confirmBtn = screen.getByRole('button', { name: /^delete$/i })
    await user.click(confirmBtn)
    expect(scanner.scannerApi.deleteDefinition).toHaveBeenCalledWith(2)
  })

  it('calls onOpenChange(false) on cancel', async () => {
    const user = userEvent.setup()
    const onOpenChange = vi.fn()
    render(
      <Wrapper>
        <DeleteDialog open={true} onOpenChange={onOpenChange} definition={CLONE_DEF} />
      </Wrapper>
    )
    const cancelBtn = screen.getByRole('button', { name: /cancel/i })
    await user.click(cancelBtn)
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })
})
