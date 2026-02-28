import { apiSlice } from "../../services/apiSlice";
import type { User, UserInput } from "./userTypes";

export interface UserMetadata{
  plan:{
    name:string,
    slug:string,
    id:number,
  }
  plan_features:{
    [key:string]:{
      limit_type:string,
      limit_value:number,
      service_area:string
      service_identifier:string
      usage_value?: number
    }
  }
}

export interface MfaSetupResponse {
  mfa_secret: string;
  otpauth_url: string;
  qr_code: string;
  mfa_enabled: boolean;
}

export interface MfaVerifyResponse {
  detail: string;
  mfa_enabled: boolean;
}

const userApiSlice = apiSlice.injectEndpoints({
  endpoints: (builder) => ({
    retrieveUser: builder.query<User, void>({
      query: () => "/users/me/",
    }),
    
    updateUser: builder.mutation<User, Partial<UserInput>>({
      query: (body) => ({
        url: "/users/me/",
        method: "PATCH",
        body,
      }),
    }),
    setPassword: builder.mutation<void, { current_password: string; new_password: string }>({
      query: (body) => ({
        url: "/users/set_password/",
        method: "POST",
        body,
      }),
    }),
    getUserMetaData: builder.query<UserMetadata, void>({
      query: () => ({
        url: "/accounts/users/quota-meta-data/",
        service:'users'
      }),
    }),
    resendActivation: builder.mutation<{ detail: string }, { email: string }>({
      query: ({ email }) => ({
        url: `/users/resend_activation/`,
        method: "POST",
        body: { email },
        service:'users'
      }),
    }),

    mfaSetup: builder.mutation<MfaSetupResponse, { force?: boolean }>({
      query: (body) => ({
        url: "/accounts/mfa/setup/",
        method: "POST",
        body,
        service: "users",
      }),
    }),
    getLoggedInUser: builder.query<User, void>({
      query: () => ({
        url: "/users/me/",
        service:'users'
      }),
    }),
    mfaVerify: builder.mutation<MfaVerifyResponse, { code: string }>({
      query: (body) => ({
        url: "/accounts/mfa/verify/",
        method: "POST",
        body,
        service: "users",
      }),
    }),
  }),
});

export const {
  useRetrieveUserQuery,
  useUpdateUserMutation,
  useSetPasswordMutation,
  useGetUserMetaDataQuery,
  useResendActivationMutation,
  useMfaSetupMutation,
  useMfaVerifyMutation,
  useGetLoggedInUserQuery,
} = userApiSlice;
