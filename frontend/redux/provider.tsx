"use client"

import { makeStore } from "./store"
import { Provider } from "react-redux"
import { PersistGate } from "redux-persist/integration/react"
import { persistStore, type Persistor } from "redux-persist"
import { useRef } from "react"
import type React from "react"


interface Props {
  children: React.ReactNode
}

export default function StoreProvider({ children }: Props) {
  const storeRef = useRef<ReturnType<typeof makeStore> | null>(null)
  const persistorRef = useRef<Persistor | null>(null)
  if (!storeRef.current) {
    storeRef.current = makeStore()
    persistorRef.current = persistStore(storeRef.current)
  }

  return (
    <Provider store={storeRef.current}>
      <PersistGate loading={null} persistor={persistorRef.current!}>
        {children}
      </PersistGate>
    </Provider>
  )
}
