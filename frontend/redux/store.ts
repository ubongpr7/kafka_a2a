import { combineReducers, configureStore } from "@reduxjs/toolkit"
import { persistReducer, FLUSH, REHYDRATE, PAUSE, PERSIST, PURGE, REGISTER } from "redux-persist"
import createWebStorage from "redux-persist/lib/storage/createWebStorage"
import globalReducer from "./state"
import ka2aReducer from "./features/ka2a/ka2aSlice"
import { apiSlice } from "./services/apiSlice"

const createNoopStorage = () => ({
  getItem() {
    return Promise.resolve(null)
  },
  setItem(_key: string, value: unknown) {
    return Promise.resolve(value)
  },
  removeItem() {
    return Promise.resolve()
  },
})

const storage = typeof window === "undefined" ? createNoopStorage() : createWebStorage("local")

// Persist config for global slice only
const globalPersistConfig = {
  key: "global",
  storage,
  whitelist: ["isDarkMode", "isSidebarCollapsed"],
}

const ka2aPersistConfig = {
  key: "ka2a",
  storage,
  whitelist: ["sessions", "activeSessionId"],
}

const rootReducer = combineReducers({
  [apiSlice.reducerPath]: apiSlice.reducer,
  global: persistReducer(globalPersistConfig, globalReducer),
  ka2a: persistReducer(ka2aPersistConfig, ka2aReducer),
})

export const makeStore = () => {
  return configureStore({
    reducer: rootReducer,
    middleware: (getDefaultMiddleware) =>
      getDefaultMiddleware({
        serializableCheck: {
          ignoredActions: [FLUSH, REHYDRATE, PAUSE, PERSIST, PURGE, REGISTER],
        },
      }).concat(apiSlice.middleware),
  })
}

export type AppStore = ReturnType<typeof makeStore>
export type RootState = ReturnType<AppStore["getState"]>
export type AppDispatch = AppStore["dispatch"]
