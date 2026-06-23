import { fireEvent } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@/test/test-utils'
import { ParamForm } from '../ParamForm'

describe('ParamForm', () => {
  it('renders 10 input fields for buy screener_type', () => {
    render(<ParamForm screenerType="buy" value={{}} onChange={vi.fn()} />)
    const inputs = screen.getAllByRole('spinbutton') // number inputs
    expect(inputs).toHaveLength(10)
  })

  it('renders 7 input fields for sell screener_type', () => {
    render(<ParamForm screenerType="sell" value={{}} onChange={vi.fn()} />)
    const inputs = screen.getAllByRole('spinbutton')
    expect(inputs).toHaveLength(7)
  })

  it('shows default value when key not in value prop', () => {
    render(<ParamForm screenerType="buy" value={{}} onChange={vi.fn()} />)
    const gapInput = screen.getByLabelText(/gap %/i) as HTMLInputElement
    expect(gapInput.value).toBe('3')
  })

  it('shows overridden value when key present in value prop', () => {
    render(<ParamForm screenerType="buy" value={{ gap_pct: 1.5 }} onChange={vi.fn()} />)
    const gapInput = screen.getByLabelText(/gap %/i) as HTMLInputElement
    expect(gapInput.value).toBe('1.5')
  })

  it('calls onChange with updated params when input changes', () => {
    const handleChange = vi.fn()
    render(<ParamForm screenerType="buy" value={{ gap_pct: 3 }} onChange={handleChange} />)
    const gapInput = screen.getByLabelText(/gap %/i)
    // Use fireEvent.change for reliable number input testing in jsdom
    fireEvent.change(gapInput, { target: { value: '2.5' } })
    expect(handleChange).toHaveBeenCalled()
    const lastCall = handleChange.mock.calls[handleChange.mock.calls.length - 1][0]
    expect(lastCall.gap_pct).toBe(2.5)
  })

  it('disables all inputs when disabled prop is true', () => {
    render(<ParamForm screenerType="buy" value={{}} onChange={vi.fn()} disabled={true} />)
    const inputs = screen.getAllByRole('spinbutton')
    for (const input of inputs) {
      expect(input).toBeDisabled()
    }
  })
})
