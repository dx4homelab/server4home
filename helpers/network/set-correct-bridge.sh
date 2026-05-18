# 1) Create the bridge with DHCP
sudo nmcli con add type bridge ifname br0 con-name br0 stp no \
    ipv4.method auto ipv6.method auto

# 2) Enslave enp7s0 to br0
sudo nmcli con add type ethernet ifname enp7s0 con-name br0-port-enp7s0 \
    master br0 slave-type bridge

# 3) Stop the old standalone profile from grabbing enp7s0
sudo nmcli con modify "Wired connection 1" connection.autoconnect no

# 4) Switch over (br0 will request DHCP via enp7s0)
sudo nmcli con down "Wired connection 1"
sudo nmcli con up br0
