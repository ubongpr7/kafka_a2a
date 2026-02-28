export interface User {
  id?: number;
  first_name: string;
  last_name: string;
  email: string;
  has_onboarded?: boolean;
  created_at?: string;
  updated_at?: string;
}

export type UserInput = Omit<User, |
"id" | "email"  | "created_at" | "updated_at" >
