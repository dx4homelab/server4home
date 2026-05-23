KC=./kubeconfigs/k3s-test-on-virt-manager.kubeconfig
kubectl --kubeconfig $KC get nodes -o wide
kubectl --kubeconfig $KC get pods -A
kubectl --kubeconfig $KC -n kube-system get svc traefik     # VIP from MetalLB
kubectl --kubeconfig $KC -n cattle-system get pods,ingress  # Rancher health
