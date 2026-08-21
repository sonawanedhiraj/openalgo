import { afterEach, describe, expect, it, vi } from 'vitest'
import { webClient } from '@/api/client'
import { ZerodhaTotpHelper } from '@/components/broker/ZerodhaTotpHelper'
import { render, screen, userEvent, waitFor } from '@/test/test-utils'

const CODE = '123456'

/** Loosened axios spy type — the real generic signature is unusable in test doubles. */
type AxiosStub = ReturnType<typeof vi.fn>

function mockStatus(configured: boolean) {
  return (vi.spyOn(webClient, 'get') as AxiosStub).mockImplementation((url: string) => {
    if (url.includes('/status')) {
      return Promise.resolve({ data: { status: 'success', broker: 'zerodha', configured } })
    }
    return Promise.resolve({
      data: {
        status: 'success',
        broker: 'zerodha',
        code: CODE,
        seconds_remaining: 21,
        interval: 30,
      },
    })
  })
}

describe('ZerodhaTotpHelper', () => {
  afterEach(() => vi.restoreAllMocks())

  it('shows the live code and countdown when a secret is configured', async () => {
    mockStatus(true)
    const onCodeChange = vi.fn()

    render(<ZerodhaTotpHelper onCodeChange={onCodeChange} />)

    await waitFor(() => expect(screen.getByTestId('totp-code')).toHaveTextContent('123 456'))
    expect(screen.getByText('21s')).toBeInTheDocument()
    // Parent gets the raw code so Connect can auto-copy it.
    expect(onCodeChange).toHaveBeenCalledWith(CODE)
  })

  it('offers the setup input when no secret is stored', async () => {
    mockStatus(false)

    render(<ZerodhaTotpHelper />)

    await waitFor(() => expect(screen.getByPlaceholderText(/Base32 secret/i)).toBeInTheDocument())
    expect(screen.queryByTestId('totp-code')).not.toBeInTheDocument()
  })

  it('saves a pasted secret and switches to the live-code view', async () => {
    mockStatus(false)
    const postSpy = (vi.spyOn(webClient, 'post') as AxiosStub).mockResolvedValue({
      data: { status: 'success' },
    })

    render(<ZerodhaTotpHelper />)

    const input = await screen.findByPlaceholderText(/Base32 secret/i)
    await userEvent.type(input, 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP')
    await userEvent.click(screen.getByRole('button', { name: /Save/i }))

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith('/api/broker-totp', {
        broker: 'zerodha',
        secret: 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP', // pragma: allowlist secret
      })
    )
    await waitFor(() => expect(screen.getByTestId('totp-code')).toHaveTextContent('123 456'))
  })

  it('masks the secret input so it is never shown in the clear', async () => {
    mockStatus(false)
    render(<ZerodhaTotpHelper />)
    const input = await screen.findByPlaceholderText(/Base32 secret/i)
    expect(input).toHaveAttribute('type', 'password')
  })
})
