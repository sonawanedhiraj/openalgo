import { afterEach, describe, expect, it, vi } from 'vitest'
import { webClient } from '@/api/client'
import { ZerodhaAutoLogin } from '@/components/broker/ZerodhaAutoLogin'
import { render, screen, userEvent, waitFor } from '@/test/test-utils'

type AxiosStub = ReturnType<typeof vi.fn>

function mockStatus(overrides: Partial<Record<string, unknown>> = {}) {
  return (vi.spyOn(webClient, 'get') as AxiosStub).mockResolvedValue({
    data: {
      status: 'success',
      broker: 'zerodha',
      enabled: false,
      has_credentials: false,
      has_totp: false,
      user_id: null,
      live: false,
      ...overrides,
    },
  })
}

describe('ZerodhaAutoLogin', () => {
  afterEach(() => vi.restoreAllMocks())

  it('offers the setup form when no credentials are stored', async () => {
    mockStatus({ has_credentials: false })
    render(<ZerodhaAutoLogin />)
    await waitFor(() => expect(screen.getByPlaceholderText(/Kite user ID/i)).toBeInTheDocument())
  })

  it('saves user-id + password then shows the auto-login button', async () => {
    // status: first call has none, refetch after save reports stored + totp.
    const getSpy = vi.spyOn(webClient, 'get') as AxiosStub
    getSpy
      .mockResolvedValueOnce({
        data: {
          status: 'success',
          enabled: false,
          has_credentials: false,
          has_totp: true,
          user_id: null,
          live: false,
        },
      })
      .mockResolvedValue({
        data: {
          status: 'success',
          enabled: false,
          has_credentials: true,
          has_totp: true,
          user_id: 'AB1234',
          live: false,
        },
      })
    const postSpy = (vi.spyOn(webClient, 'post') as AxiosStub).mockResolvedValue({
      data: { status: 'success' },
    })

    render(<ZerodhaAutoLogin />)
    await userEvent.type(await screen.findByPlaceholderText(/Kite user ID/i), 'AB1234')
    await userEvent.type(screen.getByPlaceholderText(/Kite login password/i), 'pw123') // pragma: allowlist secret
    await userEvent.click(screen.getByRole('button', { name: /^Save$/i }))

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith('/api/broker-auto-login/credentials', {
        broker: 'zerodha',
        user_id: 'AB1234',
        password: 'pw123', // pragma: allowlist secret
      })
    )
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Auto login now/i })).toBeInTheDocument()
    )
  })

  it('password input is masked', async () => {
    mockStatus({ has_credentials: false })
    render(<ZerodhaAutoLogin />)
    const input = await screen.findByPlaceholderText(/Kite login password/i)
    expect(input).toHaveAttribute('type', 'password')
  })

  it('disables auto-login when the TOTP key is missing', async () => {
    mockStatus({ has_credentials: true, has_totp: false, user_id: 'AB1234' })
    render(<ZerodhaAutoLogin />)
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Auto login now/i })).toBeDisabled()
    )
  })
})
