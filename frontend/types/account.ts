export interface UserData {
  id: number;
  email?: string;
  username?: string;
  first_name?: string;
  last_name?: string;
  is_active?: boolean;
  is_staff?: boolean;
  [key: string]: unknown;
}

export interface UserProfileData {
  id?: number;
  user?: number;
  phone_number?: string;
  address?: string;
  city?: string;
  country?: string;
  [key: string]: unknown;
}

export interface WalletData {
  id?: number;
  user?: number;
  balance?: number;
  currency?: string;
  address?: string;
  [key: string]: unknown;
}

export interface ReferralData {
  id?: number;
  referrer?: number;
  referred_user?: number;
  code?: string;
  status?: string;
  created_at?: string;
  [key: string]: unknown;
}

export interface KYCRewardRequest {
  wallet_address?: string;
  chain?: string;
  [key: string]: unknown;
}

export interface KYCRewardResponse {
  success?: boolean;
  message?: string;
  tx_hash?: string;
  amount?: number | string;
  [key: string]: unknown;
}
