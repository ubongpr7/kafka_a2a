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

Make sure the Gateway is running on `http://localhost:8000` (or update `KA2A_GATEWAY_URL` in `.env.local`).

## Features

- Streaming chat via `POST /stream` (SSE)
- Session memory via `contextId` reuse
- Event timeline (task/status/artifact updates)

## Scripts

- `yarn dev` - start development server
- `yarn build` - production build
- `yarn start` - run production server
- `yarn lint` - lint app
