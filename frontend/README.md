# K-A2A Playground UI

Next.js + Redux UI for interacting with the K-A2A FastAPI Gateway.

## Quick Start

```bash
cd frontend
yarn install
cp .env.example .env.local
yarn dev
```

Open http://localhost:3000.

Set `NEXT_PUBLIC_KA2A_GATEWAY_URL` in `.env.local` to the gateway you want the browser to call directly, for example `https://dev.agents.interaims.com`.

## Features

- Streaming chat via `POST /stream` (SSE)
- Session memory via `contextId` reuse
- Event timeline (task/status/artifact updates)

## Scripts

- `yarn dev` - start development server
- `yarn build` - production build
- `yarn start` - run production server
- `yarn lint` - lint app
