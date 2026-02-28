import { useEffect } from 'react';
import { useRouter, usePathname } from 'next/navigation';
import { useGetLoggedInUserQuery } from './userApiSlice';

export interface UserData {
  id: string;
  email: string;
  name: string;
}
export const publicRoutes = ['/accounts/signin','/','','/accounts/signin/verify', '/accounts','/accounts/verify', '/accounts/forgot-password'];

export const useAuth = () => {
  const router = useRouter();
  const pathname = usePathname();
  const isPublic = publicRoutes.includes(pathname);

  const { data: user, isLoading, isSuccess } = useGetLoggedInUserQuery(undefined, {
    skip: isPublic,
    refetchOnMountOrArgChange: true,
  });

  return {
    user: user as UserData | undefined,
    isLoading: isLoading && !isPublic,
    isAuthenticated: !!user,
    isPublic,
  };
};
