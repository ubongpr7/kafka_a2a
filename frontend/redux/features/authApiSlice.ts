import { getCookie } from "cookies-next"
import { apiSlice } from "../services/apiSlice"
import { act } from "react"

interface User {
  first_name: string
  last_name: string
  email: string
}

interface SocialAuthArgs {
  provider: string
  state: string
  code: string
}

interface CreateUserResponse {
  success: boolean
  user: User
}

const refreshToken = getCookie("refreshToken")

const authApiSlice = apiSlice.injectEndpoints({
  endpoints: (builder) => ({
    retrieveUser: builder.query<User, void>({
      query: () => "/auth-api/users/me/",
    }),
    socialAuthenticate: builder.mutation<CreateUserResponse, SocialAuthArgs>({
      query: ({ provider, state, code }) => ({
        url: `/o/${provider}/?state=${encodeURIComponent(state)}&code=${encodeURIComponent(code)}`,
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/x-www-form-urlencoded",
        },
      }),
    }),

    login: builder.mutation({
      query: ({ email, password }) => ({
        url: "/jwt/create/",
        method: "POST",
        body: { email, password },
      }),
    }),
    djoserVerifyAccount: builder.mutation({
      query: (body) => ({
        url: "/auth-api/users/activation/",
        method: "POST",
        body: body,
      }),
    }),

    register: builder.mutation({
      query: ({  email, password, re_password, first_name, last_name }) => ({
        url: "/auth-api/users/",
        method: "POST",
        body: {  email, password, re_password, first_name, last_name },
      }),
    }),

    getUserActiveStatus: builder.query({
      query: (user_id) => ({
        url: `api/v1/accounts/check-active/${user_id}/`
      }),
    }),
    
    verify: builder.mutation({
      query: () => ({
        url: "/jwt/verify/",
        method: "POST",
      }),
    }),
    resendCode: builder.mutation<void, { email: string,action:string }>({
      query: (data) => ({
        url: '/api/v1/accounts/verify/',
        method: 'POST',
        body: data
      })
    }),
    
    verifyCode: builder.mutation<void, { email: string; code: string,action:string }>({
      query: (data) => ({
        url: '/api/v1/accounts/verify/',
        method: 'POST',
        body: data
      })
    }),
    logout: builder.mutation({
      query: () => ({
        url: "/api/v1/accounts/logout/",
        method: "POST",
        body: { refresh: refreshToken },
      }),
    }),
    activation: builder.mutation({
      query: ({ uid, token }) => ({
        url: "/users/activation/",
        method: "POST",
        body: { uid, token },
      }),
    }),
    resetPassword: builder.mutation({
      query: ({ email }) => ({
        url: "/auth-api/users/reset_password/",
        method: "POST",
        body: { email },
      }),
    }),
    resetPasswordConfirm: builder.mutation({
      query: ({ uid, token, new_password, re_new_password }) => ({
        url: "/auth-api/users/reset_password_confirm/",
        method: "POST",
        body: { uid, token, new_password, re_new_password },
      }),
    }),
  }),
})

export const {
  useRetrieveUserQuery,
  useSocialAuthenticateMutation,
  useLoginMutation,
  useGetUserActiveStatusQuery,
  useRegisterMutation,
  useVerifyMutation,
  useVerifyCodeMutation,
  useDjoserVerifyAccountMutation,
  useResendCodeMutation,
  useLogoutMutation,
  useActivationMutation,
  useResetPasswordMutation,
  useResetPasswordConfirmMutation,
} = authApiSlice
