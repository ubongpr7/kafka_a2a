import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react"
import type { BaseQueryFn, FetchArgs as OriginalFetchArgs, FetchBaseQueryError } from "@reduxjs/toolkit/query"
import { setAuth, logout } from "../features/authSlice"
import { Mutex } from "async-mutex"
import { setCookie, getCookie, deleteCookie } from "cookies-next"
const BACKEND_HOST_URL = process.env.NEXT_PUBLIC_BACKEND_HOST_URL ?? ''

export type serviceType = "users" | "support"
const accessAge = 60*60*24
const refreshAge = 60*60*24
export const serviceMap: Record<serviceType, string> = {
  users: BACKEND_HOST_URL,
  support: BACKEND_HOST_URL,
}

const mutex = new Mutex()

interface FetchArgs extends OriginalFetchArgs {
  meta?: {
    isFileUpload?: boolean
  }
  service?: serviceType
}

// Create base queries for each service
const createBaseQuery = (baseUrl: string, isFileUpload = false) => {
  return fetchBaseQuery({
    baseUrl,
    credentials: "include",
    timeout: 600000,
    prepareHeaders: (headers, { getState }) => {
      const token = getCookie("accessToken")

      if (token) {
        headers.set("Authorization", `Bearer ${token}`)
      }


      if (!isFileUpload) {
        headers.set("Content-Type", "application/json")
      }
      headers.set("X-Requested-With", "XMLHttpRequest")
      return headers
    },
  })
}

const baseQueries = {
  users: createBaseQuery(serviceMap.users),
  support: createBaseQuery(serviceMap.support),
}

const fileUploadQueries = {
  users: createBaseQuery(serviceMap.users, true),
  support: createBaseQuery(serviceMap.support, true),
}

const isFileUpload = (args: string | FetchArgs): boolean => {
  if (typeof args === "string") return false
  if (args.body instanceof FormData) return true
  return args.meta?.isFileUpload === true
}

const getServiceForEndpoint = (args: string | FetchArgs): serviceType => {
  if (typeof args === "string") {
    return "users"
  }

  if (args.service) {
    return args.service
  }

  const url = args.url
  if (url.includes("/support_api/")) {
    return "support"
  }
  if (url.includes("/jwt/") || url.includes("/api/v1/accounts/")) {
    return "users"
  }
  return "users"
}

const baseQueryWithReauth: BaseQueryFn<string | FetchArgs, unknown, FetchBaseQueryError> = async (
  args,
  api,
  extraOptions,
) => {
  await mutex.waitForUnlock()

  const argsObj = typeof args === "string" ? { url: args } : args
  const service = getServiceForEndpoint(argsObj)
  const isUpload = isFileUpload(argsObj)

  const appropriateBaseQuery = isUpload ? fileUploadQueries[service] : baseQueries[service]

  const enhancedArgs = {
    ...argsObj,
    mode: "cors" as RequestMode,
  }

  let result = await appropriateBaseQuery(enhancedArgs, api, extraOptions)

  if (result?.data && service === "users") {
    const url = enhancedArgs.url
    if (url === "/jwt/create/" || url === "/jwt/refresh/") {
      const response = result.data as {
        access: string
        refresh: string
        access_token: string
        id: string
        wallet_address: string
        is_staff: boolean

      }

      setCookie("accessToken", response.access, { maxAge:  accessAge,httpOnly: false, path: "/",sameSite: 'lax' })
      setCookie("refreshToken", response.refresh, { maxAge:   refreshAge, path: "/" })
      setCookie('walletAddress',response.wallet_address,{ maxAge:   refreshAge, path: "/" })
      setCookie('is_staff',response.is_staff,{ maxAge:   refreshAge, path: "/" })
      api.dispatch(setAuth())
    } else if (url === "/api/v1/accounts/logout/") {
      api.dispatch(logout())
    }
  }

  if (result.error) {
    if (result.error.status === "FETCH_ERROR" && result.error.error?.includes("CORS")) {
    }

    if (result.error.status === 401) {
      if (!mutex.isLocked()) {
        const release = await mutex.acquire()
        try {
          const refreshToken = getCookie("refreshToken")

          if (refreshToken) {
            const refreshResult = await fetchBaseQuery({
                  baseUrl:BACKEND_HOST_URL,
                  credentials: "include",
                  prepareHeaders: (headers, { getState }) => {
              
              headers.set("Content-Type", "application/json");
              headers.set("X-Requested-With", "XMLHttpRequest");
              return headers
    },
            })(
                  
              {
                url: "/jwt/refresh/",
                method: "POST",
                body: { refresh: refreshToken },
                mode: "cors",
              },

              api,
              extraOptions,
            )

            if (refreshResult.data) {
              const data = refreshResult.data as { access: string,refresh:string }
              const newAccessToken = data.access
              
              const newRefreshToken = data.refresh
              setCookie("accessToken", newAccessToken, { maxAge:  accessAge,httpOnly: false, path: "/",sameSite: 'lax' })

              setCookie("refreshToken", newRefreshToken, { maxAge:   refreshAge, path: "/" })
              
              api.dispatch(setAuth())
              result = await appropriateBaseQuery(enhancedArgs, api, extraOptions)
            } else {
              deleteCookie("accessToken")
              deleteCookie("refreshToken")
              api.dispatch(logout())
            }
          } else {
            api.dispatch(logout())
            window.location.href='/accounts/signin'
          }
        } finally {
          release()
        }
      } else {
        await mutex.waitForUnlock()
        result = await appropriateBaseQuery(enhancedArgs, api, extraOptions)
      }
    }
  }

  return result
}

export const apiSlice = createApi({
  reducerPath: "api",
  baseQuery: baseQueryWithReauth,
  tagTypes: ["User", "Inventory", "Category", "SupportTicket"],
  endpoints: (builder) => ({}),
})

export const createFileUploadRequest = (
  url: string,
  formData: FormData,
  method: "POST" | "PATCH" | "PUT" = "POST",
  service?: serviceType,
): FetchArgs => {
  return {
    url,
    method,
    body: formData,
    meta: { isFileUpload: true },
    service,
    mode: "cors",
  }
}

export const createServiceRequest = (
  url: string,
  method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE" = "GET",
  service: serviceType,
  body?: unknown,
): FetchArgs => {
  return {
    url,
    method,
    body,
    service,
    mode: "cors",
  }
}
